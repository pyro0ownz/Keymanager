#!/usr/bin/env python3
"""
OpenClaw Key Rotation Daemon v2.0
Watches OpenClaw logs in real time and auto-rotates API keys
when rate limits or errors are detected.

Run alongside OpenClaw:
  python3 key_rotator.py watch &

Or use the other commands:
  python3 key_rotator.py status   - Show all key status
  python3 key_rotator.py rotate   - Force rotate to next key
  python3 key_rotator.py reset    - Reset all error counts
  python3 key_rotator.py test     - Test current active key
  python3 key_rotator.py watch    - Start auto-rotation daemon
"""

import json
import time
import os
import sys
import subprocess
import signal
import re
from pathlib import Path
from datetime import datetime

# ================================================================
#  CONFIGURATION
# ================================================================

# Paths
OPENCLAW_DIR = os.path.expanduser("~/.openclaw")
AGENT_DIR = os.path.join(OPENCLAW_DIR, "agents/main/agent")
AUTH_PROFILES = os.path.join(AGENT_DIR, "auth-profiles.json")
AUTH_JSON = os.path.join(AGENT_DIR, "auth.json")
OPENCLAW_JSON = os.path.join(OPENCLAW_DIR, "openclaw.json")

# Log patterns that indicate rate limiting or key errors
# These match Google's API error responses
RATE_LIMIT_PATTERNS = [
    "RESOURCE_EXHAUSTED",
    "api key limit reached",
    "rate limit",
    "quota exceeded",
    "429",
    "Too Many Requests",
    "rateLimitExceeded",
    "RATE_LIMIT_EXCEEDED",
    "dailyLimitExceeded",
    "userRateLimitExceeded",
]

KEY_ERROR_PATTERNS = [
    "API_KEY_INVALID",
    "PERMISSION_DENIED",
    "API key not valid",
    "API key expired",
    "INVALID_API_KEY",
]

# How long to wait before reusing a rate-limited key (seconds)
COOLDOWN_SECONDS = 65

# How often the watcher checks for log updates (seconds)
WATCH_INTERVAL = 2

# Minimum time between rotations to prevent rapid cycling (seconds)
MIN_ROTATION_INTERVAL = 5


# ================================================================
#  KEY ROTATOR CLASS
# ================================================================

class KeyRotator:
    def __init__(self, profiles_path=None):
        self.profiles_path = Path(profiles_path or AUTH_PROFILES)
        self.auth_json_path = Path(AUTH_JSON)
        self.openclaw_json_path = Path(OPENCLAW_JSON)
        self.last_rotation_time = 0
        self.load()

    def load(self):
        """Load profiles from disk."""
        try:
            with open(self.profiles_path, 'r') as f:
                self.data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            print(f"  XX Cannot load {self.profiles_path}: {e}")
            sys.exit(1)

    def save(self):
        """Save profiles to disk."""
        try:
            with open(self.profiles_path, 'w') as f:
                json.dump(self.data, f, indent=2)
        except Exception as e:
            print(f"  XX Cannot save {self.profiles_path}: {e}")

    def get_provider_keys(self, provider='google'):
        """Get all keys for a provider, sorted by score (lowest = best)."""
        keys = []
        now = time.time()
        for name, profile in self.data.get('profiles', {}).items():
            prov = profile.get('provider', name.split(':')[0])
            if prov != provider:
                continue
            stats = self.data.get('usageStats', {}).get(name, {})
            error_count = stats.get('errorCount', 0)
            last_failure = stats.get('lastFailureAt', 0)

            # Score calculation:
            # - Base score = error count
            # - If failed within cooldown window, add heavy penalty
            # - If key is marked dead (100+ errors), add massive penalty
            score = error_count
            if last_failure > 0 and (now - last_failure) < COOLDOWN_SECONDS:
                score += 10000  # In cooldown - push to bottom
            if error_count >= 100:
                score += 100000  # Dead key - effectively disabled

            keys.append({
                'score': score,
                'name': name,
                'key': profile.get('key', ''),
                'errors': error_count,
                'last_failure': last_failure,
                'in_cooldown': last_failure > 0 and (now - last_failure) < COOLDOWN_SECONDS,
            })

        keys.sort(key=lambda x: x['score'])
        return keys

    def get_best_key(self, provider='google'):
        """Get the best available key for the provider."""
        keys = self.get_provider_keys(provider)
        if not keys:
            return None, None

        best = keys[0]

        # Warn if all keys are in cooldown
        available = [k for k in keys if not k['in_cooldown']]
        if not available:
            cooldown_remaining = []
            for k in keys:
                remaining = COOLDOWN_SECONDS - (time.time() - k['last_failure'])
                cooldown_remaining.append(max(0, int(remaining)))
            min_wait = min(cooldown_remaining)
            print(f"  [!!] All {len(keys)} keys in cooldown. Next available in {min_wait}s")
            # Return the one closest to coming out of cooldown
            keys.sort(key=lambda x: x['last_failure'])
            best = keys[0]

        return best['name'], best['key']

    def rotate(self, provider='google', reason="manual"):
        """Mark current key as failed and rotate to next."""
        now = time.time()

        # Throttle rapid rotations
        if (now - self.last_rotation_time) < MIN_ROTATION_INTERVAL:
            return None, None

        self.load()  # Refresh from disk

        # Mark current key as failed
        current = self.data.get('lastGood', {}).get(provider)
        if current and current in self.data.get('usageStats', {}):
            stats = self.data['usageStats'][current]
            stats['errorCount'] = stats.get('errorCount', 0) + 1
            stats['lastFailureAt'] = now

        # Get next best key
        name, key = self.get_best_key(provider)
        if not name or not key:
            print(f"  XX No keys available for {provider}")
            return None, None

        # Update tracking
        self.data.setdefault('lastGood', {})[provider] = name
        self.data.setdefault('usageStats', {}).setdefault(name, {})
        self.data['usageStats'][name]['lastUsed'] = now
        self.save()

        # Update auth.json
        self._update_auth_json(provider, key)

        # Update openclaw.json env var
        self._update_env(provider, key)

        self.last_rotation_time = now
        ts = datetime.now().strftime('%H:%M:%S')
        print(f"  [{ts}] Rotated {provider}: {current} -> {name} (reason: {reason})")
        return name, key

    def _update_auth_json(self, provider, key):
        """Update auth.json with new active key."""
        try:
            if self.auth_json_path.exists():
                with open(self.auth_json_path) as f:
                    auth = json.load(f)
            else:
                auth = {}
            auth[provider] = {"type": "api_key", "key": key}
            with open(self.auth_json_path, 'w') as f:
                json.dump(auth, f, indent=2)
        except Exception as e:
            print(f"  XX auth.json update failed: {e}")

    def _update_env(self, provider, key):
        """Update the env var in openclaw.json so it persists across restarts."""
        try:
            if not self.openclaw_json_path.exists():
                return
            with open(self.openclaw_json_path) as f:
                config = json.load(f)

            # Find the env var name for this provider
            env_map = {
                'google': 'GOOGLE_API_KEY',
                'groq': 'GROQ_API_KEY',
                'nvidia-nim': 'NVIDIA_API_KEY',
                'openrouter': 'OPENROUTER_API_KEY',
                'mistral': 'MISTRAL_API_KEY',
                'together': 'TOGETHER_API_KEY',
                'cerebras': 'CEREBRAS_API_KEY',
                'sambanova': 'SAMBANOVA_API_KEY',
                'deepseek': 'DEEPSEEK_API_KEY',
                'hyperbolic': 'HYPERBOLIC_API_KEY',
            }
            env_var = env_map.get(provider)
            if env_var and 'env' in config:
                config['env'][env_var] = key
                with open(self.openclaw_json_path, 'w') as f:
                    json.dump(config, f, indent=2)
        except Exception as e:
            print(f"  XX openclaw.json env update failed: {e}")

    def report_success(self, provider='google'):
        """Report successful API call. Resets error count for current key."""
        self.load()
        current = self.data.get('lastGood', {}).get(provider)
        if current and current in self.data.get('usageStats', {}):
            self.data['usageStats'][current]['errorCount'] = 0
            self.save()

    def reset_all(self, provider=None):
        """Reset all error counts. If provider specified, only that provider."""
        self.load()
        for name in self.data.get('usageStats', {}):
            if provider and not name.startswith(f"{provider}:"):
                continue
            self.data['usageStats'][name]['errorCount'] = 0
            self.data['usageStats'][name]['lastFailureAt'] = 0
        self.save()
        scope = provider or "all providers"
        print(f"  OK Reset error counts for {scope}")

    def status(self):
        """Print status table of all keys."""
        self.load()
        now = time.time()

        # Get current active keys
        auth = {}
        try:
            if self.auth_json_path.exists():
                with open(self.auth_json_path) as f:
                    auth = json.load(f)
        except:
            pass

        # Group by provider
        by_provider = {}
        for name, profile in self.data.get('profiles', {}).items():
            prov = profile.get('provider', name.split(':')[0])
            by_provider.setdefault(prov, []).append(name)

        stats = self.data.get('usageStats', {})

        for prov, names in sorted(by_provider.items()):
            active_key = auth.get(prov, {}).get('key', '')
            print(f"\n  -- {prov} ({len(names)} keys) --")
            print(f"  {'Profile':<22} {'Err':<5} {'Cooldown':<12} {'Status'}")
            print(f"  {'-'*60}")

            for name in sorted(names):
                key = self.data['profiles'][name].get('key', '?')
                s = stats.get(name, {})
                errs = s.get('errorCount', 0)
                last_fail = s.get('lastFailureAt', 0)

                # Status icon
                if errs == 0:
                    icon = "[OK]"
                elif errs <= 3:
                    icon = "[--]"
                elif errs >= 100:
                    icon = "[DEAD]"
                else:
                    icon = "[!!]"

                # Active marker
                active = " < ACTIVE" if key == active_key else ""

                # Cooldown
                if last_fail > 0 and (now - last_fail) < COOLDOWN_SECONDS:
                    remaining = int(COOLDOWN_SECONDS - (now - last_fail))
                    cd = f"{remaining}s"
                else:
                    cd = "-"

                print(f"  {name:<22} {errs:<5} {cd:<12} {icon}{active}")

        # Summary
        total_keys = sum(len(v) for v in by_provider.values())
        total_healthy = sum(
            1 for name in stats
            if stats[name].get('errorCount', 0) == 0
        )
        total_cooldown = sum(
            1 for name in stats
            if stats[name].get('lastFailureAt', 0) > 0
            and (now - stats[name]['lastFailureAt']) < COOLDOWN_SECONDS
        )
        print(f"\n  Total: {total_keys} keys | "
              f"Healthy: {total_healthy} | "
              f"In cooldown: {total_cooldown}")


# ================================================================
#  LOG WATCHER - AUTO ROTATION DAEMON
# ================================================================

class LogWatcher:
    """Watches OpenClaw output for rate limit errors and auto-rotates keys."""

    def __init__(self, rotator):
        self.rotator = rotator
        self.running = True
        self.rotation_count = 0
        self.start_time = time.time()

        # Compile patterns for fast matching
        self.rate_patterns = [re.compile(re.escape(p), re.IGNORECASE) for p in RATE_LIMIT_PATTERNS]
        self.error_patterns = [re.compile(re.escape(p), re.IGNORECASE) for p in KEY_ERROR_PATTERNS]

    def check_line(self, line):
        """Check a log line for rate limit or key errors."""
        # Check for rate limits
        for pattern in self.rate_patterns:
            if pattern.search(line):
                return "rate_limit"

        # Check for dead key errors
        for pattern in self.error_patterns:
            if pattern.search(line):
                return "key_error"

        return None

    def handle_error(self, error_type, provider='google'):
        """Handle a detected error by rotating keys."""
        if error_type == "key_error":
            # Mark key with heavy penalty - it's probably permanently dead
            self.rotator.load()
            current = self.rotator.data.get('lastGood', {}).get(provider)
            if current and current in self.rotator.data.get('usageStats', {}):
                self.rotator.data['usageStats'][current]['errorCount'] = 100
                self.rotator.save()

        name, key = self.rotator.rotate(provider, reason=error_type)
        if name:
            self.rotation_count += 1

    def watch_log_file(self, log_path):
        """Tail a log file and watch for errors."""
        print(f"  Watching: {log_path}")
        try:
            with open(log_path, 'r') as f:
                # Seek to end
                f.seek(0, 2)
                while self.running:
                    line = f.readline()
                    if line:
                        error_type = self.check_line(line)
                        if error_type:
                            self.handle_error(error_type)
                    else:
                        time.sleep(WATCH_INTERVAL)
        except FileNotFoundError:
            print(f"  XX Log file not found: {log_path}")
        except KeyboardInterrupt:
            pass

    def watch_gateway_output(self):
        """Watch OpenClaw gateway output via subprocess."""
        print(f"  Watching gateway output...")
        try:
            proc = subprocess.Popen(
                ["openclaw", "gateway", "logs", "--follow"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            while self.running:
                line = proc.stdout.readline()
                if not line:
                    break
                error_type = self.check_line(line)
                if error_type:
                    self.handle_error(error_type)
        except FileNotFoundError:
            print("  XX 'openclaw' command not found")
            print("  Falling back to log file watcher...")
            return False
        except KeyboardInterrupt:
            pass
        return True

    def watch_stdin(self):
        """Read from stdin pipe - for use with: openclaw gateway logs -f | python3 key_rotator.py watch"""
        print("  Watching stdin (pipe mode)...")
        print("  Usage: openclaw gateway logs -f | python3 key_rotator.py watch")
        try:
            for line in sys.stdin:
                if not self.running:
                    break
                error_type = self.check_line(line)
                if error_type:
                    self.handle_error(error_type)
        except KeyboardInterrupt:
            pass

    def start(self):
        """Start the watcher daemon."""
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        print(f"\n  ==========================================")
        print(f"  Key Rotation Daemon v2.0")
        print(f"  Started: {ts}")
        print(f"  Cooldown: {COOLDOWN_SECONDS}s")
        print(f"  Min rotation interval: {MIN_ROTATION_INTERVAL}s")
        print(f"  ==========================================\n")

        # Show current status
        self.rotator.status()
        print()

        # Set up clean shutdown
        def shutdown(sig, frame):
            self.running = False
            elapsed = int(time.time() - self.start_time)
            print(f"\n  Daemon stopped. Rotations: {self.rotation_count} in {elapsed}s")
            sys.exit(0)

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)

        # Determine watch method
        if not sys.stdin.isatty():
            # Being piped to - read stdin
            self.watch_stdin()
        else:
            # Try gateway logs command first
            # Look for common log locations
            log_locations = [
                os.path.join(OPENCLAW_DIR, "logs", "gateway.log"),
                os.path.join(OPENCLAW_DIR, "gateway.log"),
                "/var/log/openclaw/gateway.log",
            ]

            log_found = None
            for loc in log_locations:
                if os.path.exists(loc):
                    log_found = loc
                    break

            if log_found:
                self.watch_log_file(log_found)
            else:
                # Try subprocess
                if not self.watch_gateway_output():
                    print("\n  Could not find OpenClaw logs.")
                    print("  Use pipe mode instead:")
                    print("    openclaw gateway logs -f | python3 key_rotator.py watch")
                    print("\n  Or run in polling mode (checks key health every 30s):")
                    self.watch_polling()

    def watch_polling(self):
        """Fallback: periodically test the active key and rotate if dead."""
        print("  Running in polling mode (testing active key every 30s)...")
        while self.running:
            try:
                # Test current key with a minimal API call
                self.rotator.load()
                name, key = self.rotator.get_best_key('google')
                if key:
                    result = self._test_key(key)
                    if result == "rate_limit":
                        self.handle_error("rate_limit")
                    elif result == "key_error":
                        self.handle_error("key_error")
                    elif result == "ok":
                        self.rotator.report_success('google')
                time.sleep(30)
            except KeyboardInterrupt:
                break

    def _test_key(self, key):
        """Test a key with a minimal API request."""
        try:
            import urllib.request
            import urllib.error

            url = (f"https://generativelanguage.googleapis.com/v1beta/"
                   f"models/gemini-2.0-flash:generateContent?key={key}")
            data = json.dumps({
                "contents": [{"parts": [{"text": "ping"}]}],
                "generationConfig": {"maxOutputTokens": 5}
            }).encode()

            req = urllib.request.Request(url, data=data,
                                        headers={"Content-Type": "application/json"})
            response = urllib.request.urlopen(req, timeout=15)
            body = response.read().decode()

            if '"text"' in body:
                return "ok"
            return "unknown"

        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else ""
            if e.code == 429 or "RESOURCE_EXHAUSTED" in body:
                return "rate_limit"
            if "API_KEY_INVALID" in body or "PERMISSION_DENIED" in body:
                return "key_error"
            return "error"
        except Exception:
            return "error"


# ================================================================
#  TEST COMMAND - verify active key works
# ================================================================

def test_active_key():
    """Test the currently active key."""
    rotator = KeyRotator()
    name, key = rotator.get_best_key('google')
    if not key:
        print("  XX No keys configured")
        return

    print(f"  Testing {name} (...{key[-8:]})...")
    watcher = LogWatcher(rotator)
    result = watcher._test_key(key)

    results = {
        "ok": "[OK] Key is working",
        "rate_limit": "[!!] Key is rate limited",
        "key_error": "[XX] Key is invalid or disabled",
        "error": "[??] Could not reach API",
        "unknown": "[??] Unexpected response",
    }
    print(f"  {results.get(result, result)}")

    if result == "rate_limit":
        print("  Rotating...")
        rotator.rotate('google', reason="test_rate_limit")
    elif result == "key_error":
        print("  Marking as dead and rotating...")
        rotator.load()
        if name in rotator.data.get('usageStats', {}):
            rotator.data['usageStats'][name]['errorCount'] = 100
            rotator.save()
        rotator.rotate('google', reason="test_key_error")


# ================================================================
#  CLI
# ================================================================

def main():
    if len(sys.argv) < 2:
        print("  OpenClaw Key Rotator v2.0")
        print("")
        print("  Commands:")
        print("    python3 key_rotator.py status  - Show all key status")
        print("    python3 key_rotator.py rotate   - Force rotate to next key")
        print("    python3 key_rotator.py reset    - Reset all error counts")
        print("    python3 key_rotator.py test     - Test current active key")
        print("    python3 key_rotator.py watch    - Start auto-rotation daemon")
        print("")
        print("  Auto-rotation (pipe mode):")
        print("    openclaw gateway logs -f | python3 key_rotator.py watch")
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
        name, key = rotator.rotate(provider, reason="manual")
        if name:
            print(f"  Active key: {name} (...{key[-8:]})")

    elif cmd == 'reset':
        rotator = KeyRotator()
        provider = sys.argv[2] if len(sys.argv) > 2 else None
        rotator.reset_all(provider)

    elif cmd == 'test':
        test_active_key()

    elif cmd == 'watch':
        rotator = KeyRotator()
        watcher = LogWatcher(rotator)
        watcher.start()

    else:
        print(f"  Unknown command: {cmd}")
        print("  Try: status, rotate, reset, test, watch")


if __name__ == '__main__':
    main()
