#!/usr/bin/env python3
"""
+==================================================================+
|  OpenClaw Key Rotation Daemon v3.0                               |
|  Bucket-Aware Cooldown + Exponential Backoff                     |
|  Built for OpenClaw 2026.2.24+                                   |
+==================================================================+

Reads bucket metadata from auth-profiles.json (set by Key Manager v4.0).
On 429/rate limit: cools down the entire BUCKET (Google project), not
just one key. Picks next key from a different bucket.

Only writes auth.json for rotation (confirmed: takes effect without
gateway restart). Does NOT touch openclaw.json env or device.json.

Usage:
  python3 key_rotator.py status    Show all keys + bucket cooldowns
  python3 key_rotator.py rotate    Force rotate to next bucket/key
  python3 key_rotator.py reset     Reset all cooldowns and error counts
  python3 key_rotator.py test      Test current active key health
  python3 key_rotator.py health    Quick health ping (no rotation)
  python3 key_rotator.py watch     Start auto-rotation daemon

Watch modes:
  openclaw logs --follow | python3 key_rotator.py watch    (pipe, fastest)
  python3 key_rotator.py watch                              (auto-detect)

Log locations checked:
  /tmp/openclaw/openclaw-YYYY-MM-DD.log   (OpenClaw default)
  Pipe from: openclaw logs --follow       (CLI tail)
"""

import json
import time
import os
import sys
import subprocess
import signal
import re
import fcntl
import tempfile
import random
from pathlib import Path
from datetime import datetime

# ================================================================
#  CONFIGURATION
# ================================================================

OPENCLAW_DIR = os.path.expanduser("~/.openclaw")
AGENT_DIR = os.path.join(OPENCLAW_DIR, "agents/main/agent")
AUTH_PROFILES = os.path.join(AGENT_DIR, "auth-profiles.json")
AUTH_JSON = os.path.join(AGENT_DIR, "auth.json")

# Backoff policy (Krill spec)
BACKOFF_BASE_SECONDS = 15
BACKOFF_MAX_SECONDS = 600
BACKOFF_JITTER_MAX = 2.0

# Key-level cooldown for non-bucket providers
KEY_COOLDOWN_SECONDS = 65

# Minimum time between rotations to prevent rapid cycling
MIN_ROTATION_INTERVAL = 5

# Polling interval for active checker mode
POLL_INTERVAL = 30

# Log patterns that indicate rate limiting
RATE_LIMIT_PATTERNS = [
    "RESOURCE_EXHAUSTED",
    "API rate limit reached",
    "rate limit",
    "quota exceeded",
    "Too Many Requests",
    "rateLimitExceeded",
    "RATE_LIMIT_EXCEEDED",
    "dailyLimitExceeded",
    "userRateLimitExceeded",
]

# Patterns that indicate key is dead (manual intervention needed)
KEY_DEAD_PATTERNS = [
    "API_KEY_INVALID",
    "PERMISSION_DENIED",
    "API key not valid",
    "API key expired",
    "INVALID_API_KEY",
]

# Short cooldown errors (network/5xx)
TRANSIENT_PATTERNS = [
    "INTERNAL",
    "UNAVAILABLE",
    "DEADLINE_EXCEEDED",
]


# ================================================================
#  ATOMIC FILE I/O (matches Key Manager v4.0)
# ================================================================

def load_json(path):
    """Load JSON with shared lock."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, 'r') as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                return json.load(f)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except (json.JSONDecodeError, PermissionError) as e:
        print(f"  XX Cannot load {path}: {e}")
        return {}


def save_json(path, data):
    """Atomic save: temp file + fsync + rename. With exclusive lock."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    dir_name = os.path.dirname(path)
    try:
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix='.tmp')
        with os.fdopen(fd, 'w') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
            fcntl.flock(f, fcntl.LOCK_UN)
        os.rename(tmp_path, path)
    except Exception as e:
        if 'tmp_path' in locals() and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        print(f"  XX Failed to write {path}: {e}")


# ================================================================
#  KEY ROTATOR CLASS (bucket-aware)
# ================================================================

class KeyRotator:
    def __init__(self, profiles_path=None):
        self.profiles_path = Path(profiles_path or AUTH_PROFILES)
        self.auth_json_path = Path(AUTH_JSON)
        self.last_rotation_time = 0
        self.load()

    def load(self):
        """Refresh from disk."""
        self.data = load_json(str(self.profiles_path))

    def save(self):
        """Write profiles to disk (atomic)."""
        save_json(str(self.profiles_path), self.data)

    # ---- Bucket cooldown logic ----

    def _bucket_key(self, provider, bucket):
        return f"{provider}/{bucket}"

    def _get_bucket_stats(self, provider, bucket):
        bk = self._bucket_key(provider, bucket)
        return self.data.get("bucketStats", {}).get(bk, {
            "cooldownUntilMs": 0, "consecutive429": 0, "last429AtMs": 0
        })

    def _set_bucket_cooldown(self, provider, bucket):
        """Set exponential backoff cooldown for a bucket.
        cooldown = min(600s, 15s * 2^consecutive429) + jitter"""
        bk = self._bucket_key(provider, bucket)
        self.data.setdefault("bucketStats", {})
        bs = self.data["bucketStats"].setdefault(bk, {
            "cooldownUntilMs": 0, "consecutive429": 0, "last429AtMs": 0
        })

        bs["consecutive429"] = bs.get("consecutive429", 0) + 1
        bs["last429AtMs"] = int(time.time() * 1000)

        n = bs["consecutive429"]
        cooldown = min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * (2 ** (n - 1)))
        jitter = random.uniform(0, BACKOFF_JITTER_MAX)
        total = cooldown + jitter

        bs["cooldownUntilMs"] = int((time.time() + total) * 1000)
        return total

    def _clear_bucket_cooldown(self, provider, bucket):
        bk = self._bucket_key(provider, bucket)
        if bk in self.data.get("bucketStats", {}):
            self.data["bucketStats"][bk]["consecutive429"] = 0
            self.data["bucketStats"][bk]["cooldownUntilMs"] = 0

    def _is_bucket_cooling(self, provider, bucket):
        bs = self._get_bucket_stats(provider, bucket)
        return bs.get("cooldownUntilMs", 0) > time.time() * 1000

    # ---- Key selection ----

    def get_provider_keys(self, provider='google'):
        """Get all keys for a provider sorted by score (lower = better)."""
        now_ms = int(time.time() * 1000)
        keys = []
        for name, profile in self.data.get('profiles', {}).items():
            prov = profile.get('provider', name.split(':')[0])
            if prov != provider:
                continue

            bucket = profile.get('bucket', 'default')
            stats = self.data.get('usageStats', {}).get(name, {})
            error_count = stats.get('errorCount', 0)
            last_used = stats.get('lastUsed', 0)

            bucket_cooling = self._is_bucket_cooling(provider, bucket)

            last_fail = stats.get('lastFailureAt', 0)
            key_cooling = False
            if last_fail > 0:
                key_cooling = (now_ms - last_fail * 1000) < (KEY_COOLDOWN_SECONDS * 1000)

            score = 0
            if error_count >= 100:
                score += 1000000
            elif bucket_cooling:
                score += 100000
            elif key_cooling:
                score += 10000
            score += error_count
            score += last_used / 1e15 if last_used else 0

            keys.append({
                'score': score,
                'name': name,
                'key': profile.get('key', ''),
                'bucket': bucket,
                'errors': error_count,
                'bucket_cooling': bucket_cooling,
                'key_cooling': key_cooling,
                'last_used': last_used,
                'dead': error_count >= 100,
            })

        keys.sort(key=lambda x: x['score'])
        return keys

    def get_best_key(self, provider='google', _skip_reload=False):
        """Get the best available key, preferring non-cooling buckets."""
        if not _skip_reload:
            self.load()
        keys = self.get_provider_keys(provider)
        if not keys:
            return None, None, None

        best = keys[0]

        available = [k for k in keys if not k['bucket_cooling'] and not k['dead']]
        if not available:
            now_ms = int(time.time() * 1000)
            soonest_wait = float('inf')
            for k in keys:
                if k['dead']:
                    continue
                bs = self._get_bucket_stats(provider, k['bucket'])
                cd = bs.get('cooldownUntilMs', 0)
                wait = (cd - now_ms) / 1000
                if 0 < wait < soonest_wait:
                    soonest_wait = wait
                    best = k

            if 0 < soonest_wait < float('inf'):
                print(f"  [!!] All buckets cooling. Next available in {int(soonest_wait)}s")

        return best['name'], best['key'], best['bucket']

    def rotate(self, provider='google', reason="manual"):
        """Mark current key/bucket as failed and rotate to next bucket."""
        now = time.time()

        if (now - self.last_rotation_time) < MIN_ROTATION_INTERVAL:
            return None, None, None

        self.load()

        current_name = self.data.get('lastGood', {}).get(provider)
        current_bucket = "default"
        if current_name and current_name in self.data.get('profiles', {}):
            current_bucket = self.data['profiles'][current_name].get('bucket', 'default')
            stats = self.data.setdefault('usageStats', {}).setdefault(current_name, {})
            stats['errorCount'] = stats.get('errorCount', 0) + 1
            stats['lastFailureAt'] = now

        cd_seconds = self._set_bucket_cooldown(provider, current_bucket)

        # Save cooldown state before selecting next key
        self.save()

        name, key, bucket = self.get_best_key(provider, _skip_reload=True)
        if not name or not key:
            print(f"  XX No keys available for {provider}")
            self.save()
            return None, None, None

        self.data.setdefault('lastGood', {})[provider] = name
        self.data.setdefault('usageStats', {}).setdefault(name, {})
        self.data['usageStats'][name]['lastUsed'] = now
        self.save()

        self._update_auth_json(provider, key)

        self.last_rotation_time = now
        ts = datetime.now().strftime('%H:%M:%S')
        bucket_changed = bucket != current_bucket
        switch = f"bucket {current_bucket} -> {bucket}" if bucket_changed else "same bucket"
        print(f"  [{ts}] Rotated {provider}: {current_name} -> {name} "
              f"({switch}, cooldown {int(cd_seconds)}s, reason: {reason})")
        return name, key, bucket

    def mark_dead(self, provider='google', profile_name=None):
        """Mark a key as dead (100+ errors). For 401/403 responses."""
        self.load()
        if profile_name is None:
            profile_name = self.data.get('lastGood', {}).get(provider)
        if profile_name and profile_name in self.data.get('usageStats', {}):
            self.data['usageStats'][profile_name]['errorCount'] = 100
            self.save()
            ts = datetime.now().strftime('%H:%M:%S')
            print(f"  [{ts}] Marked {profile_name} as DEAD (auth error)")

    def report_success(self, provider='google'):
        """Report successful API call. Clears bucket cooldown + resets errors."""
        self.load()
        current = self.data.get('lastGood', {}).get(provider)
        if current and current in self.data.get('profiles', {}):
            bucket = self.data['profiles'][current].get('bucket', 'default')
            self._clear_bucket_cooldown(provider, bucket)
            if current in self.data.get('usageStats', {}):
                self.data['usageStats'][current]['errorCount'] = 0
            self.save()

    def _update_auth_json(self, provider, key):
        """Update auth.json with new active key (atomic write)."""
        try:
            auth = load_json(str(self.auth_json_path))
            auth[provider] = {"type": "api_key", "key": key}
            save_json(str(self.auth_json_path), auth)
        except Exception as e:
            print(f"  XX auth.json update failed: {e}")

    def reset_all(self, provider=None):
        """Reset all error counts and bucket cooldowns."""
        self.load()
        for name in self.data.get('usageStats', {}):
            if provider and not name.startswith(f"{provider}:"):
                continue
            self.data['usageStats'][name]['errorCount'] = 0
            self.data['usageStats'][name]['lastFailureAt'] = 0

        for bk in self.data.get('bucketStats', {}):
            if provider and not bk.startswith(f"{provider}/"):
                continue
            self.data['bucketStats'][bk]['cooldownUntilMs'] = 0
            self.data['bucketStats'][bk]['consecutive429'] = 0
            self.data['bucketStats'][bk]['last429AtMs'] = 0

        self.save()
        scope = provider or "all providers"
        print(f"  OK Reset all cooldowns and errors for {scope}")

    def status(self):
        """Print status table with bucket cooldown info."""
        self.load()
        now = time.time()
        now_ms = int(now * 1000)

        auth = load_json(str(self.auth_json_path))

        by_provider = {}
        for name, profile in self.data.get('profiles', {}).items():
            prov = profile.get('provider', name.split(':')[0])
            by_provider.setdefault(prov, []).append(name)

        stats = self.data.get('usageStats', {})
        bucket_stats = self.data.get('bucketStats', {})

        for prov, names in sorted(by_provider.items()):
            active_key = auth.get(prov, {}).get('key', '')
            print(f"\n  -- {prov} ({len(names)} keys) --")

            prov_buckets = {k: v for k, v in bucket_stats.items()
                           if k.startswith(f"{prov}/")}
            if prov_buckets:
                for bk, bs in sorted(prov_buckets.items()):
                    cd_until_ms = bs.get('cooldownUntilMs', 0)
                    consec = bs.get('consecutive429', 0)
                    bucket_name = bk.split('/', 1)[1] if '/' in bk else bk
                    if cd_until_ms > now_ms:
                        remaining = int((cd_until_ms - now_ms) / 1000)
                        print(f"    BUCKET [{bucket_name}]: COOLING {remaining}s "
                              f"(streak: {consec})")
                    elif consec > 0:
                        print(f"    BUCKET [{bucket_name}]: ready "
                              f"(last streak: {consec})")
                    else:
                        print(f"    BUCKET [{bucket_name}]: [OK]")

            print(f"  {'Name':<24} {'Err':<5} {'Bucket':<12} {'Status'}")
            print(f"  {'-' * 60}")

            for name in sorted(names):
                key = self.data['profiles'][name].get('key', '?')
                bucket = self.data['profiles'][name].get('bucket', 'default')
                s = stats.get(name, {})
                errs = s.get('errorCount', 0)

                if errs == 0:
                    icon = "[OK]"
                elif errs >= 100:
                    icon = "[DEAD]"
                elif errs <= 3:
                    icon = "[--]"
                else:
                    icon = "[!!]"

                active = " < ACTIVE" if key == active_key else ""
                print(f"  {name:<24} {errs:<5} {bucket:<12} {icon}{active}")

        total = sum(len(v) for v in by_provider.values())
        healthy = sum(1 for s in stats.values() if s.get('errorCount', 0) == 0)
        cooling = sum(1 for bs in bucket_stats.values()
                      if bs.get('cooldownUntilMs', 0) > now_ms)
        dead = sum(1 for s in stats.values() if s.get('errorCount', 0) >= 100)
        print(f"\n  Total: {total} keys | Healthy: {healthy} | "
              f"Buckets cooling: {cooling} | Dead: {dead}")


# ================================================================
#  LOG WATCHER - AUTO ROTATION DAEMON
# ================================================================

class LogWatcher:
    def __init__(self, rotator):
        self.rotator = rotator
        self.running = True
        self.rotation_count = 0
        self.start_time = time.time()

        self.rate_patterns = [re.compile(re.escape(p), re.IGNORECASE)
                              for p in RATE_LIMIT_PATTERNS]
        self.dead_patterns = [re.compile(re.escape(p), re.IGNORECASE)
                              for p in KEY_DEAD_PATTERNS]
        self.transient_patterns = [re.compile(re.escape(p), re.IGNORECASE)
                                   for p in TRANSIENT_PATTERNS]

    def classify_line(self, line):
        for p in self.dead_patterns:
            if p.search(line):
                return "dead"
        for p in self.rate_patterns:
            if p.search(line):
                return "rate_limit"
        for p in self.transient_patterns:
            if p.search(line):
                return "transient"
        return None

    def handle_error(self, error_type, provider='google'):
        if error_type == "dead":
            self.rotator.mark_dead(provider)
            name, key, bucket = self.rotator.rotate(provider, reason="key_dead")
            if name:
                self.rotation_count += 1
        elif error_type == "rate_limit":
            name, key, bucket = self.rotator.rotate(provider, reason="429_rate_limit")
            if name:
                self.rotation_count += 1
        elif error_type == "transient":
            ts = datetime.now().strftime('%H:%M:%S')
            print(f"  [{ts}] Transient error (5xx/timeout) - monitoring")

    def watch_stdin(self):
        print("  Watching stdin (pipe mode)...")
        try:
            for line in sys.stdin:
                if not self.running:
                    break
                error_type = self.classify_line(line)
                if error_type:
                    self.handle_error(error_type)
        except KeyboardInterrupt:
            pass

    def watch_log_file(self, log_path):
        print(f"  Watching: {log_path}")
        try:
            with open(log_path, 'r') as f:
                f.seek(0, 2)
                while self.running:
                    line = f.readline()
                    if line:
                        error_type = self.classify_line(line)
                        if error_type:
                            self.handle_error(error_type)
                    else:
                        time.sleep(2)
        except FileNotFoundError:
            print(f"  XX Log file not found: {log_path}")
            return False
        except KeyboardInterrupt:
            pass
        return True

    def watch_subprocess(self):
        print("  Watching via: openclaw logs --follow")
        try:
            proc = subprocess.Popen(
                ["openclaw", "logs", "--follow"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            while self.running:
                line = proc.stdout.readline()
                if not line:
                    break
                error_type = self.classify_line(line)
                if error_type:
                    self.handle_error(error_type)
            proc.terminate()
        except FileNotFoundError:
            print("  XX 'openclaw' command not found in PATH")
            return False
        except KeyboardInterrupt:
            pass
        return True

    def watch_polling(self):
        print(f"  Polling mode: testing active key every {POLL_INTERVAL}s...")
        while self.running:
            try:
                self.rotator.load()
                name, key, bucket = self.rotator.get_best_key('google')
                if key:
                    result = self._test_key(key)
                    ts = datetime.now().strftime('%H:%M:%S')
                    if result == "rate_limit":
                        print(f"  [{ts}] Active key rate limited")
                        self.handle_error("rate_limit")
                    elif result == "dead":
                        print(f"  [{ts}] Active key is dead")
                        self.handle_error("dead")
                    elif result == "ok":
                        self.rotator.report_success('google')
                time.sleep(POLL_INTERVAL)
            except KeyboardInterrupt:
                break

    def _test_key(self, key):
        try:
            import urllib.request
            import urllib.error

            url = (f"https://generativelanguage.googleapis.com/v1beta/"
                   f"models/gemini-2.0-flash:generateContent?key={key}")
            data = json.dumps({
                "contents": [{"parts": [{"text": "ping"}]}],
                "generationConfig": {"maxOutputTokens": 5}
            }).encode()

            req = urllib.request.Request(
                url, data=data,
                headers={"Content-Type": "application/json"}
            )
            response = urllib.request.urlopen(req, timeout=15)
            body = response.read().decode()
            if '"text"' in body:
                return "ok"
            return "unknown"

        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode()
            except:
                pass
            if e.code == 429 or "RESOURCE_EXHAUSTED" in body:
                return "rate_limit"
            if "API_KEY_INVALID" in body or "PERMISSION_DENIED" in body:
                return "dead"
            if e.code >= 500:
                return "transient"
            return "error"
        except Exception:
            return "error"

    def start(self):
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        print(f"\n  ==========================================")
        print(f"  Key Rotation Daemon v3.0")
        print(f"  Started: {ts}")
        print(f"  Backoff: {BACKOFF_BASE_SECONDS}s base, "
              f"{BACKOFF_MAX_SECONDS}s max, exponential")
        print(f"  ==========================================\n")

        self.rotator.status()
        print()

        def shutdown(sig, frame):
            self.running = False
            elapsed = int(time.time() - self.start_time)
            print(f"\n  Daemon stopped. Rotations: {self.rotation_count} "
                  f"in {elapsed}s")
            sys.exit(0)

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)

        if not sys.stdin.isatty():
            self.watch_stdin()
            return

        today = datetime.now().strftime('%Y-%m-%d')
        log_locations = [
            f"/tmp/openclaw/openclaw-{today}.log",
            os.path.join(OPENCLAW_DIR, "logs", "gateway.log"),
        ]

        for loc in log_locations:
            if os.path.exists(loc):
                if self.watch_log_file(loc):
                    return

        if self.watch_subprocess():
            return

        print("\n  Could not find OpenClaw logs. Using polling mode.")
        print("  For faster reaction: openclaw logs --follow | python3 key_rotator.py watch")
        print()
        self.watch_polling()


# ================================================================
#  HEALTH / TEST COMMANDS
# ================================================================

def health_check():
    rotator = KeyRotator()
    name, key, bucket = rotator.get_best_key('google')
    if not key:
        print("  XX No keys configured")
        return
    print(f"  Pinging {name} (bucket: {bucket})...")
    watcher = LogWatcher(rotator)
    result = watcher._test_key(key)
    msg = {
        "ok": "[OK] Key is working",
        "rate_limit": "[!!] Key is rate limited",
        "dead": "[XX] Key is invalid or disabled",
        "transient": "[!!] Transient error (5xx)",
        "error": "[??] Could not reach API",
        "unknown": "[??] Unexpected response",
    }
    print(f"  {msg.get(result, result)}")
    return result


def test_and_rotate():
    rotator = KeyRotator()
    name, key, bucket = rotator.get_best_key('google')
    if not key:
        print("  XX No keys configured")
        return
    print(f"  Testing {name} (...{key[-8:]}, bucket: {bucket})...")
    watcher = LogWatcher(rotator)
    result = watcher._test_key(key)
    msg = {
        "ok": "[OK] Key is working",
        "rate_limit": "[!!] Key is rate limited",
        "dead": "[XX] Key is invalid or disabled",
        "transient": "[!!] Transient error",
        "error": "[??] Could not reach API",
    }
    print(f"  {msg.get(result, result)}")
    if result == "rate_limit":
        print("  Rotating (bucket cooldown)...")
        rotator.rotate('google', reason="test_429")
    elif result == "dead":
        print("  Marking as dead and rotating...")
        rotator.mark_dead('google', name)
        rotator.rotate('google', reason="test_dead")
    elif result == "ok":
        rotator.report_success('google')
        print("  No rotation needed")


# ================================================================
#  CLI
# ================================================================

def main():
    if len(sys.argv) < 2:
        print("  OpenClaw Key Rotator v3.0")
        print("  Bucket-aware cooldown + exponential backoff")
        print("")
        print("  Commands:")
        print("    python3 key_rotator.py status   Show keys + bucket cooldowns")
        print("    python3 key_rotator.py rotate    Force rotate to next bucket")
        print("    python3 key_rotator.py reset     Reset all cooldowns/errors")
        print("    python3 key_rotator.py test      Test active key + rotate if needed")
        print("    python3 key_rotator.py health    Quick health ping (no rotation)")
        print("    python3 key_rotator.py watch     Start auto-rotation daemon")
        print("")
        print("  Daemon modes:")
        print("    openclaw logs --follow | python3 key_rotator.py watch")
        print("    python3 key_rotator.py watch   (auto-detect logs/polling)")
        print("")
        rotator = KeyRotator()
        rotator.status()
        return

    cmd = sys.argv[1].lower()

    if cmd == 'status':
        rotator = KeyRotator()
        rotator.status()
    elif cmd == 'rotate':
        rotator = KeyRotator()
        provider = sys.argv[2] if len(sys.argv) > 2 else 'google'
        name, key, bucket = rotator.rotate(provider, reason="manual")
        if name:
            print(f"  Active: {name} (...{key[-8:]}) bucket: {bucket}")
    elif cmd == 'reset':
        rotator = KeyRotator()
        provider = sys.argv[2] if len(sys.argv) > 2 else None
        rotator.reset_all(provider)
    elif cmd == 'test':
        test_and_rotate()
    elif cmd == 'health':
        health_check()
    elif cmd == 'watch':
        rotator = KeyRotator()
        watcher = LogWatcher(rotator)
        watcher.start()
    else:
        print(f"  Unknown command: {cmd}")
        print("  Try: status, rotate, reset, test, health, watch")


if __name__ == '__main__':
    main()
