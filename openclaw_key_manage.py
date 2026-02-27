#!/usr/bin/env python3
"""
+==================================================================+
|  OpenClaw Key Manager v4.0                                       |
|  Multi-Provider Key Pool + Bucket-Aware Rotation                 |
|  Built for OpenClaw 2026.2.24+                                   |
+==================================================================+

Files touched:
  ~/.openclaw/agents/main/agent/auth-profiles.json   (key pool + stats + buckets)
  ~/.openclaw/agents/main/agent/auth.json             (active key)
  ~/.openclaw/agents/main/agent/models.json           (provider defs)
  ~/.openclaw/openclaw.json                           (env, auth, models, whitelist)
  ~/.openclaw/device.json                             (Ed25519 identity - OPT-IN only)

Changelog v4.0 (from Krill review + upstream PR prep):
  - ADDED: Bucket/project tagging in keys.txt (# bucket=projA)
  - ADDED: bucketStats in auth-profiles.json for project-level cooldown
  - ADDED: Atomic writes (temp file + rename) to prevent corruption
  - ADDED: File locking to prevent race conditions with rotator daemon
  - CHANGED: Device rotation is now OPT-IN (--rotate-device flag)
  - CHANGED: --status counts models from whitelist, not models.json
  - FIXED: Duplicate-keys-only crash in step_auth_profiles()
  - FIXED: read_keys() now returns structured entries with bucket metadata
  - KEPT: Google as native provider (no models.providers injection)
  - KEPT: All v3.2 features (--fix, --status, --remove, pre-flight)
"""

import json, os, sys, shutil, subprocess, hashlib, secrets, time, fcntl, tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


@contextmanager
def file_lock(target_path):
    """Cross-process lock for a config file.
    Locks target_path + '.lock' so atomic rename doesn't break the lock."""
    lock_path = target_path + ".lock"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with open(lock_path, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)

# ===================================================================
#  Ed25519 KEY GENERATION (multi-method fallback)
# ===================================================================

def _ed25519_generate():
    """Generate Ed25519 keypair. Tries three methods in order."""
    errors = []

    # Method 1: cryptography library (preferred)
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import (
            Encoding, PrivateFormat, PublicFormat, NoEncryption
        )
        priv = Ed25519PrivateKey.generate()
        pub_pem = priv.public_key().public_bytes(
            Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
        ).decode()
        priv_pem = priv.private_bytes(
            Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
        ).decode()
        return pub_pem, priv_pem
    except ImportError:
        errors.append("cryptography: not installed")
    except Exception as e:
        errors.append(f"cryptography: {e}")

    # Method 2: openssl CLI
    try:
        r = subprocess.run(
            ["openssl", "genpkey", "-algorithm", "Ed25519", "-outform", "PEM"],
            capture_output=True, text=True, timeout=10
        )
        if r.returncode == 0 and "BEGIN PRIVATE KEY" in r.stdout:
            priv_pem = r.stdout
            r2 = subprocess.run(
                ["openssl", "pkey", "-pubout"],
                input=priv_pem, capture_output=True, text=True, timeout=10
            )
            if r2.returncode == 0:
                return r2.stdout, priv_pem
        errors.append(f"openssl: returncode {r.returncode}")
    except FileNotFoundError:
        errors.append("openssl: not found in PATH")
    except Exception as e:
        errors.append(f"openssl: {e}")

    # Method 3: PyNaCl
    try:
        import nacl.signing
        import base64
        sk = nacl.signing.SigningKey.generate()
        vk = sk.verify_key
        priv_pem = (
            "-----BEGIN PRIVATE KEY-----\n"
            + base64.b64encode(
                b'\x30\x2e\x02\x01\x00\x30\x05\x06\x03\x2b\x65\x70'
                b'\x04\x22\x04\x20' + bytes(sk)
            ).decode()
            + "\n-----END PRIVATE KEY-----\n"
        )
        pub_pem = (
            "-----BEGIN PUBLIC KEY-----\n"
            + base64.b64encode(
                b'\x30\x2a\x30\x05\x06\x03\x2b\x65\x70'
                b'\x03\x21\x00' + bytes(vk)
            ).decode()
            + "\n-----END PUBLIC KEY-----\n"
        )
        return pub_pem, priv_pem
    except ImportError:
        errors.append("PyNaCl: not installed")
    except Exception as e:
        errors.append(f"PyNaCl: {e}")

    print("\n  XX Cannot generate Ed25519 keys. Tried:")
    for e in errors:
        print(f"    - {e}")
    print("\n  Fix: pip install cryptography --break-system-packages")
    print("  Or:  apt install openssl")
    sys.exit(1)


def generate_device():
    """Generate a new device.json matching OpenClaw's format."""
    pub_pem, priv_pem = _ed25519_generate()
    device_id = hashlib.sha256(secrets.token_bytes(64)).hexdigest()
    return {
        "version": 1,
        "deviceId": device_id,
        "publicKeyPem": pub_pem,
        "privateKeyPem": priv_pem,
        "createdAtMs": int(time.time() * 1000)
    }


# ===================================================================
#  PROVIDER CATALOG
# ===================================================================

PROVIDERS = {
    "google": {
        "name": "Google Gemini (AI Studio)",
        "api": "native",
        "url": None,
        "prefix": "AIzaSy",
        "free": True,
        "native": True,
        "info": "15 RPM, 1M TPD free | ai.google.dev",
        "env": "GOOGLE_API_KEY",
        "models": [
            {"id": "gemini-2.0-flash", "name": "Gemini 2.0 Flash",
             "cw": 1048576, "mt": 8192, "r": False},
            {"id": "gemini-2.5-flash", "name": "Gemini 2.5 Flash",
             "cw": 1048576, "mt": 65536, "r": True},
            {"id": "gemini-2.5-flash-lite", "name": "Gemini 2.5 Flash Lite",
             "cw": 1048576, "mt": 65536, "r": False},
            {"id": "gemini-3-flash-preview", "name": "Gemini 3 Flash Preview",
             "cw": 1048576, "mt": 65536, "r": True},
            {"id": "gemini-flash-latest", "name": "Gemini Flash Latest",
             "cw": 1048576, "mt": 8192, "r": False},
            {"id": "gemini-flash-lite-latest", "name": "Gemini Flash Lite Latest",
             "cw": 1048576, "mt": 8192, "r": False},
        ]
    },
    "groq": {
        "name": "Groq",
        "api": "openai-completions",
        "url": "https://api.groq.com/openai/v1",
        "prefix": "gsk_",
        "free": True,
        "native": False,
        "info": "30 RPM free | console.groq.com",
        "env": "GROQ_API_KEY",
        "models": [
            {"id": "llama-3.3-70b-versatile", "name": "LLaMA 3.3 70B",
             "cw": 128000, "mt": 32768, "r": False},
            {"id": "llama-3.1-8b-instant", "name": "LLaMA 3.1 8B",
             "cw": 128000, "mt": 8192, "r": False},
            {"id": "gemma2-9b-it", "name": "Gemma 2 9B",
             "cw": 8192, "mt": 8192, "r": False},
            {"id": "mixtral-8x7b-32768", "name": "Mixtral 8x7B",
             "cw": 32768, "mt": 32768, "r": False},
            {"id": "deepseek-r1-distill-llama-70b", "name": "DeepSeek R1 Distill 70B",
             "cw": 128000, "mt": 16384, "r": True},
            {"id": "qwen-qwq-32b", "name": "Qwen QWQ 32B",
             "cw": 128000, "mt": 16384, "r": True},
        ]
    },
    "nvidia-nim": {
        "name": "NVIDIA NIM",
        "api": "openai-completions",
        "url": "https://integrate.api.nvidia.com/v1",
        "prefix": "nvapi-",
        "free": True,
        "native": False,
        "info": "1000 req/day free | build.nvidia.com",
        "env": "NVIDIA_API_KEY",
        "models": [
            {"id": "moonshotai/kimi-k2.5", "name": "Kimi K2.5",
             "cw": 200000, "mt": 8192, "r": False},
            {"id": "nvidia/llama-3.1-nemotron-70b-instruct", "name": "Nemotron 70B",
             "cw": 131072, "mt": 4096, "r": False},
            {"id": "meta/llama-3.3-70b-instruct", "name": "Meta LLaMA 3.3 70B",
             "cw": 131072, "mt": 4096, "r": False},
            {"id": "nvidia/llama-3.1-405b-instruct", "name": "LLaMA 3.1 405B",
             "cw": 128000, "mt": 4096, "r": False},
            {"id": "nvidia/mistral-nemo-minitron-8b-8k-instruct", "name": "Mistral NeMo 8B",
             "cw": 8192, "mt": 2048, "r": False},
            {"id": "deepseek-ai/deepseek-r1", "name": "DeepSeek R1",
             "cw": 64000, "mt": 8192, "r": True},
            {"id": "mistralai/mistral-large-2-instruct", "name": "Mistral Large 2",
             "cw": 128000, "mt": 4096, "r": False},
            {"id": "qwen/qwen2.5-72b-instruct", "name": "Qwen 2.5 72B",
             "cw": 128000, "mt": 4096, "r": False},
        ]
    },
    "openrouter": {
        "name": "OpenRouter",
        "api": "openai-completions",
        "url": "https://openrouter.ai/api/v1",
        "prefix": "sk-or-",
        "free": True,
        "native": False,
        "info": "Free models | openrouter.ai",
        "env": "OPENROUTER_API_KEY",
        "models": [
            {"id": "google/gemini-2.0-flash-exp:free", "name": "Gemini 2.0 Flash (Free)",
             "cw": 1048576, "mt": 8192, "r": False},
            {"id": "deepseek/deepseek-r1:free", "name": "DeepSeek R1 (Free)",
             "cw": 164000, "mt": 16384, "r": True},
            {"id": "meta-llama/llama-3.3-70b-instruct:free", "name": "LLaMA 3.3 70B (Free)",
             "cw": 128000, "mt": 8192, "r": False},
            {"id": "qwen/qwen3-235b-a22b:free", "name": "Qwen 3 235B (Free)",
             "cw": 40960, "mt": 8192, "r": True},
        ]
    },
    "mistral": {
        "name": "Mistral AI",
        "api": "openai-completions",
        "url": "https://api.mistral.ai/v1",
        "prefix": "",
        "free": True,
        "native": False,
        "info": "Free tier | console.mistral.ai",
        "env": "MISTRAL_API_KEY",
        "models": [
            {"id": "mistral-large-latest", "name": "Mistral Large",
             "cw": 128000, "mt": 8192, "r": False},
            {"id": "codestral-latest", "name": "Codestral",
             "cw": 256000, "mt": 8192, "r": False},
            {"id": "open-mistral-nemo", "name": "Mistral Nemo",
             "cw": 128000, "mt": 8192, "r": False},
        ]
    },
    "together": {
        "name": "Together AI",
        "api": "openai-completions",
        "url": "https://api.together.xyz/v1",
        "prefix": "",
        "free": True,
        "native": False,
        "info": "$5 free | api.together.ai",
        "env": "TOGETHER_API_KEY",
        "models": [
            {"id": "meta-llama/Llama-3.3-70B-Instruct-Turbo", "name": "LLaMA 3.3 70B Turbo",
             "cw": 128000, "mt": 8192, "r": False},
            {"id": "deepseek-ai/DeepSeek-R1", "name": "DeepSeek R1",
             "cw": 164000, "mt": 16384, "r": True},
            {"id": "Qwen/Qwen2.5-72B-Instruct-Turbo", "name": "Qwen 2.5 72B",
             "cw": 128000, "mt": 8192, "r": False},
        ]
    },
    "cerebras": {
        "name": "Cerebras",
        "api": "openai-completions",
        "url": "https://api.cerebras.ai/v1",
        "prefix": "csk-",
        "free": True,
        "native": False,
        "info": "30 RPM free | cloud.cerebras.ai",
        "env": "CEREBRAS_API_KEY",
        "models": [
            {"id": "llama-3.3-70b", "name": "LLaMA 3.3 70B",
             "cw": 128000, "mt": 8192, "r": False},
            {"id": "deepseek-r1-distill-llama-70b", "name": "DeepSeek R1 Distill 70B",
             "cw": 128000, "mt": 16384, "r": True},
        ]
    },
    "sambanova": {
        "name": "SambaNova",
        "api": "openai-completions",
        "url": "https://api.sambanova.ai/v1",
        "prefix": "",
        "free": True,
        "native": False,
        "info": "Free | cloud.sambanova.ai",
        "env": "SAMBANOVA_API_KEY",
        "models": [
            {"id": "Meta-Llama-3.3-70B-Instruct", "name": "LLaMA 3.3 70B",
             "cw": 128000, "mt": 8192, "r": False},
            {"id": "DeepSeek-R1", "name": "DeepSeek R1",
             "cw": 164000, "mt": 16384, "r": True},
        ]
    },
    "deepseek": {
        "name": "DeepSeek (Direct)",
        "api": "openai-completions",
        "url": "https://api.deepseek.com/v1",
        "prefix": "sk-",
        "free": False,
        "native": False,
        "info": "$0.14/M input | platform.deepseek.com",
        "env": "DEEPSEEK_API_KEY",
        "models": [
            {"id": "deepseek-chat", "name": "DeepSeek V3",
             "cw": 164000, "mt": 16384, "r": False},
            {"id": "deepseek-reasoner", "name": "DeepSeek R1",
             "cw": 164000, "mt": 16384, "r": True},
        ]
    },
    "hyperbolic": {
        "name": "Hyperbolic",
        "api": "openai-completions",
        "url": "https://api.hyperbolic.xyz/v1",
        "prefix": "",
        "free": True,
        "native": False,
        "info": "$10 free | app.hyperbolic.xyz",
        "env": "HYPERBOLIC_API_KEY",
        "models": [
            {"id": "deepseek-ai/DeepSeek-R1", "name": "DeepSeek R1",
             "cw": 164000, "mt": 16384, "r": True},
            {"id": "Qwen/QwQ-32B", "name": "QwQ 32B",
             "cw": 128000, "mt": 16384, "r": True},
        ]
    },
}

# ===================================================================
#  PATHS
# ===================================================================

OPENCLAW_DIR   = os.path.expanduser("~/.openclaw")
AGENT_DIR      = os.path.join(OPENCLAW_DIR, "agents/main/agent")
AUTH_PROFILES  = os.path.join(AGENT_DIR, "auth-profiles.json")
AUTH_JSON      = os.path.join(AGENT_DIR, "auth.json")
MODELS_JSON    = os.path.join(AGENT_DIR, "models.json")
OPENCLAW_JSON  = os.path.join(OPENCLAW_DIR, "openclaw.json")
DEVICE_JSON    = os.path.join(OPENCLAW_DIR, "device.json")
KEYS_FILE      = "keys.txt"

# ===================================================================
#  HELPERS - ATOMIC FILE I/O WITH LOCKING
# ===================================================================

def load_json(path):
    """Load JSON file with shared lock, return empty dict if missing or corrupt."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, 'r') as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                return json.load(f)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except json.JSONDecodeError as e:
        print(f"  [!!] {os.path.basename(path)} has invalid JSON: {e}")
        print(f"       Backup will be created before overwriting.")
        return {}
    except PermissionError:
        print(f"  XX Cannot read {path} - permission denied")
        sys.exit(1)


def save_json(path, data):
    """Atomic save: write to temp file, fsync, rename. With exclusive lock."""
    os.makedirs(os.path.dirname(path), exist_ok=True)

    # Backup existing file
    if os.path.exists(path):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = f"{path}.bak.{ts}"
        shutil.copy2(path, backup)

    # Atomic write: temp file in same directory + rename
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
        print(f"    OK {os.path.basename(path)}")
    except Exception as e:
        # Clean up temp file on failure
        if 'tmp_path' in locals() and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        print(f"  XX Failed to write {path}: {e}")
        raise


def read_keys(keys_file=None):
    """Read API keys from file with optional bucket metadata.

    Format:
      AIzaSy....                    -> key with bucket="default"
      AIzaSy.... # bucket=projA     -> key with bucket="projA"
      # comment lines are skipped
      (blank lines are skipped)

    Returns list of dicts: [{"key": "...", "bucket": "..."}]
    """
    kf = keys_file or KEYS_FILE
    if not os.path.exists(kf):
        print(f"\n  XX {kf} not found")
        print(f"    Create it with one API key per line:")
        print(f"    echo 'AIzaSy... # bucket=projA' > {kf}")
        sys.exit(1)

    entries = []
    with open(kf) as f:
        for line_num, line in enumerate(f, 1):
            stripped = line.strip()
            if not stripped or stripped.startswith('#'):
                continue

            # Parse key and optional metadata from inline comment
            bucket = "default"
            key_part, sep, comment = stripped.partition("#")
            key_part = key_part.strip()
            comment = comment.strip()

            if sep:
                for token in comment.split():
                    if token.startswith("bucket="):
                        bucket = token.split("=", 1)[1]

            if key_part:
                entries.append({"key": key_part, "bucket": bucket})

    if not entries:
        print(f"\n  XX {kf} has no keys (only blank lines/comments)")
        sys.exit(1)
    return entries


def model_schema(m):
    """Build model object matching OpenClaw's expected schema."""
    return {
        "id": m["id"],
        "name": m["name"],
        "reasoning": m.get("r", False),
        "input": ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": m["cw"],
        "maxTokens": m["mt"],
    }


def build_provider_entry(pk):
    """Build a provider config entry. Omits baseUrl for native providers."""
    p = PROVIDERS[pk]
    entry = {
        "api": p["api"],
        "models": [model_schema(m) for m in p["models"]],
        "apiKey": p["env"],
    }
    if p["url"] is not None:
        entry["baseUrl"] = p["url"]
    return entry


def build_provider_entry_with_envref(pk, key):
    """Build provider entry for openclaw.json using ${ENV_VAR} reference."""
    p = PROVIDERS[pk]
    entry = {
        "apiKey": "${" + p["env"] + "}",
        "api": p["api"],
        "models": [model_schema(m) for m in p["models"]],
    }
    if p["url"] is not None:
        entry["baseUrl"] = p["url"]
    return entry


def whitelist_model_count(provider):
    """Count models from openclaw.json whitelist (agents.defaults.models).
    This is the authoritative source - what OpenClaw /models actually shows."""
    c = load_json(OPENCLAW_JSON)
    wl = c.get("agents", {}).get("defaults", {}).get("models", {})
    return sum(1 for k in wl if k.startswith(f"{provider}/"))


# ===================================================================
#  PRE-FLIGHT CHECKS
# ===================================================================

def preflight():
    """Verify OpenClaw installation exists before touching anything."""
    issues = []
    if not os.path.isdir(OPENCLAW_DIR):
        issues.append("~/.openclaw directory not found")
    if not os.path.isdir(AGENT_DIR):
        issues.append(f"Agent directory not found: {AGENT_DIR}")
    if not os.path.exists(OPENCLAW_JSON):
        issues.append("openclaw.json not found")

    if issues:
        print("\n  XX Pre-flight check failed:")
        for i in issues:
            print(f"    - {i}")
        print("\n  Is OpenClaw installed? Run: openclaw --version")
        sys.exit(1)

    try:
        with open(OPENCLAW_JSON) as f:
            json.load(f)
    except json.JSONDecodeError as e:
        print(f"\n  XX openclaw.json is corrupt: {e}")
        print(f"    Run: openclaw doctor --fix")
        sys.exit(1)

    return True


# ===================================================================
#  CONFIG REPAIR (--fix)
# ===================================================================

def fix_config():
    """Repair known v3.0/v3.2 issues in openclaw.json and models.json."""
    print("\n  +==================================================+")
    print("  |  Config Repair                                    |")
    print("  +==================================================+")

    fixed = 0

    # Fix openclaw.json
    c = load_json(OPENCLAW_JSON)
    if c:
        providers = c.get("models", {}).get("providers", {})

        if "google" in providers:
            del providers["google"]
            print("  OK Removed 'google' from models.providers (native provider)")
            fixed += 1

        for pk, prov in list(providers.items()):
            if "baseUrl" in prov and (prov["baseUrl"] is None or prov["baseUrl"] == ""):
                del prov["baseUrl"]
                print(f"  OK Removed empty baseUrl from {pk}")
                fixed += 1

        if fixed > 0:
            save_json(OPENCLAW_JSON, c)

    # Fix models.json
    m = load_json(MODELS_JSON)
    if m:
        m_fixed = 0
        m_providers = m.get("providers", {})

        if "google" in m_providers:
            del m_providers["google"]
            print("  OK Removed 'google' from models.json providers")
            m_fixed += 1

        for pk, prov in list(m_providers.items()):
            if "baseUrl" in prov and (prov["baseUrl"] is None or prov["baseUrl"] == ""):
                del prov["baseUrl"]
                print(f"  OK Removed empty baseUrl from models.json/{pk}")
                m_fixed += 1

        if m_fixed > 0:
            save_json(MODELS_JSON, m)
            fixed += m_fixed

    if fixed == 0:
        print("\n  OK No issues found - config looks clean")
    else:
        print(f"\n  OK Fixed {fixed} issue(s)")
        print("  -> Run: openclaw doctor")
        print("  -> Then: openclaw gateway restart")

    return fixed


# ===================================================================
#  STATUS DISPLAY (counts from whitelist, not models.json)
# ===================================================================

def show_status():
    """Display current state of all configured providers and keys."""
    print("\n  +==================================================+")
    print("  |  Key Pool Status                                  |")
    print("  +==================================================+")

    profiles = load_json(AUTH_PROFILES)
    auth = load_json(AUTH_JSON)

    if not profiles.get("profiles"):
        print("\n  No keys configured yet. Run the manager to add keys.")
        return

    # Group by provider
    by_provider = {}
    for alias, info in profiles.get("profiles", {}).items():
        prov = info.get("provider", "unknown")
        by_provider.setdefault(prov, []).append(alias)

    stats = profiles.get("usageStats", {})
    last_good = profiles.get("lastGood", {})
    bucket_stats = profiles.get("bucketStats", {})
    now = time.time()

    for prov, aliases in sorted(by_provider.items()):
        active_key = auth.get(prov, {}).get("key", "")
        # Count from whitelist (authoritative), not models.json
        model_count = whitelist_model_count(prov)
        print(f"\n  -- {prov} ({len(aliases)} keys, {model_count} models) --")

        # Show bucket cooldown status if any
        prov_buckets = {k: v for k, v in bucket_stats.items() if k.startswith(f"{prov}/")}
        if prov_buckets:
            for bk, bs in sorted(prov_buckets.items()):
                cd_until = bs.get("cooldownUntilMs", 0) / 1000
                consec = bs.get("consecutive429", 0)
                if cd_until > now:
                    remaining = int(cd_until - now)
                    print(f"    BUCKET {bk}: cooling {remaining}s (streak: {consec})")
                elif consec > 0:
                    print(f"    BUCKET {bk}: ready (last streak: {consec})")

        for alias in sorted(aliases):
            key = profiles["profiles"][alias].get("key", "???")
            bucket = profiles["profiles"][alias].get("bucket", "default")
            s = stats.get(alias, {})
            errs = s.get("errorCount", 0)
            last_fail = s.get("lastFailureAt", 0)

            if errs == 0:
                icon = "[OK]"
            elif errs <= 3:
                icon = "[--]"
            elif errs >= 100:
                icon = "[DEAD]"
            else:
                icon = "[!!]"

            active = " < ACTIVE" if key == active_key else ""

            cooldown = ""
            if last_fail > 0:
                elapsed = now - (last_fail / 1000 if last_fail > 1e10 else last_fail)
                if elapsed < 65:
                    cooldown = f" (cooling {65 - int(elapsed)}s)"

            bucket_tag = f" [{bucket}]" if bucket != "default" else ""
            print(f"    {icon} {alias:<22} ...{key[-8:]}  "
                  f"err:{errs}{bucket_tag}{cooldown}{active}")

        if prov in last_good:
            print(f"    Last good: {last_good[prov]}")


# ===================================================================
#  PROVIDER REMOVAL
# ===================================================================

def remove_provider(pk):
    """Cleanly remove a provider from all config files."""
    print(f"\n  Removing {pk} from all configs...")
    removed = 0

    with file_lock(AUTH_PROFILES):
        ap = load_json(AUTH_PROFILES)
        for key in list(ap.get("profiles", {}).keys()):
            if key.startswith(f"{pk}:"):
                del ap["profiles"][key]
                if key in ap.get("usageStats", {}):
                    del ap["usageStats"][key]
                removed += 1
        ap.get("lastGood", {}).pop(pk, None)
        for bk in list(ap.get("bucketStats", {}).keys()):
            if bk.startswith(f"{pk}/"):
                del ap["bucketStats"][bk]
        if removed:
            save_json(AUTH_PROFILES, ap)

    with file_lock(AUTH_JSON):
        auth = load_json(AUTH_JSON)
        if pk in auth:
            del auth[pk]
            save_json(AUTH_JSON, auth)
            removed += 1

    models = load_json(MODELS_JSON)
    if pk in models.get("providers", {}):
        del models["providers"][pk]
        save_json(MODELS_JSON, models)
        removed += 1

    c = load_json(OPENCLAW_JSON)
    p_info = PROVIDERS.get(pk, {})
    if p_info:
        c.get("env", {}).pop(p_info.get("env", ""), None)
    c.get("auth", {}).get("profiles", {}).pop(f"{pk}:default", None)
    c.get("models", {}).get("providers", {}).pop(pk, None)
    wl = c.get("agents", {}).get("defaults", {}).get("models", {})
    for key in list(wl.keys()):
        if key.startswith(f"{pk}/"):
            del wl[key]
    save_json(OPENCLAW_JSON, c)

    print(f"\n  OK Removed {pk} ({removed} entries cleaned)")
    print("  -> Run: openclaw gateway restart")


# ===================================================================
#  THE SETUP STEPS (4 by default, 5 with --rotate-device)
# ===================================================================

def step_auth_profiles(pk, key_entries):
    """Step 1: auth-profiles.json - build/merge key pool with bucket metadata.

    key_entries: list of {"key": "...", "bucket": "..."} from read_keys()
    """
    with file_lock(AUTH_PROFILES):
        d = load_json(AUTH_PROFILES)
        d.setdefault("version", 1)
        d.setdefault("profiles", {})
        d.setdefault("lastGood", {})
        d.setdefault("usageStats", {})
        d.setdefault("bucketStats", {})

        existing = [k for k in d["profiles"] if k.startswith(f"{pk}:")]
        next_idx = len(existing) + 1

        new_aliases = []
        seen_buckets = set()

        for entry in key_entries:
            key = entry["key"]
            bucket = entry["bucket"]
            seen_buckets.add(bucket)

            dupe = False
            for alias, info in d["profiles"].items():
                if info.get("key") == key:
                    if info.get("bucket") != bucket:
                        info["bucket"] = bucket
                    new_aliases.append(alias)
                    dupe = True
                    break
            if dupe:
                continue

            a = f"{pk}:key{next_idx}"
            while a in d["profiles"]:
                next_idx += 1
                a = f"{pk}:key{next_idx}"

            new_aliases.append(a)
            d["profiles"][a] = {
                "type": "api_key",
                "provider": pk,
                "key": key,
                "bucket": bucket,
            }
            d["usageStats"].setdefault(a, {
                "lastUsed": 0, "errorCount": 0, "lastFailureAt": 0
            })
            next_idx += 1

        for bucket in seen_buckets:
            bucket_key = f"{pk}/{bucket}"
            d["bucketStats"].setdefault(bucket_key, {
                "cooldownUntilMs": 0, "consecutive429": 0, "last429AtMs": 0
            })

        all_aliases = existing + [a for a in new_aliases if a not in existing]
        if all_aliases:
            d["lastGood"][pk] = all_aliases[0]
        elif new_aliases:
            d["lastGood"][pk] = new_aliases[0]
        else:
            print("  [!!] No new or existing keys for this provider")
            print("       Check keys.txt for valid entries")
            return []

        added = len([a for a in new_aliases if a not in existing])
        total = len(all_aliases)
        buckets = len(seen_buckets)
        print(f"\n  [1/4] auth-profiles.json - {added} new, {total} total, {buckets} bucket(s)")
        save_json(AUTH_PROFILES, d)
        return all_aliases


def step_auth_json(pk, key):
    """Step 2: auth.json - set active key for provider."""
    with file_lock(AUTH_JSON):
        d = load_json(AUTH_JSON)
        d[pk] = {"type": "api_key", "key": key}
        print(f"\n  [2/4] auth.json - active: ...{key[-8:]}")
        save_json(AUTH_JSON, d)


def step_models_json(pk):
    """Step 3: models.json - register provider and model definitions.
    SKIPPED for native providers (Google)."""
    p = PROVIDERS[pk]
    if p.get("native"):
        print(f"\n  [3/4] models.json - skipped (native provider)")
        return
    d = load_json(MODELS_JSON)
    d.setdefault("providers", {})
    d["providers"][pk] = build_provider_entry(pk)
    print(f"\n  [3/4] models.json - {len(p['models'])} models")
    save_json(MODELS_JSON, d)


def step_openclaw_json(pk, key):
    """Step 4: openclaw.json - env vars, auth profiles, models, whitelist.
    Native providers: env + auth + whitelist only (no models.providers)."""
    p = PROVIDERS[pk]
    c = load_json(OPENCLAW_JSON)
    if not c:
        print("  XX Cannot read openclaw.json")
        sys.exit(1)

    c.setdefault("env", {})[p["env"]] = key

    c.setdefault("auth", {}).setdefault("profiles", {})
    c["auth"]["profiles"][f"{pk}:default"] = {"provider": pk, "mode": "api_key"}

    if not p.get("native"):
        c.setdefault("models", {}).setdefault("providers", {})
        c["models"]["providers"][pk] = build_provider_entry_with_envref(pk, key)

    wl = c.setdefault("agents", {}).setdefault("defaults", {}).setdefault("models", {})
    for m in p["models"]:
        wl[f"{pk}/{m['id']}"] = {}

    native_note = "" if not p.get("native") else " (native)"
    print(f"\n  [4/4] openclaw.json - env + auth + whitelist{native_note}")
    save_json(OPENCLAW_JSON, c)


def step_device():
    """Optional: device.json - generate fresh Ed25519 device identity.
    Only runs with --rotate-device flag."""
    dev = generate_device()
    print(f"\n  [opt] device.json - new identity")
    save_json(DEVICE_JSON, dev)
    print(f"        ID: {dev['deviceId'][:16]}...")
    return dev


# ===================================================================
#  DISPLAY HELPERS
# ===================================================================

def show_providers():
    print("\n  +==================================================+")
    print("  |  Providers                                       |")
    print("  +==================================================+")
    for i, (k, p) in enumerate(PROVIDERS.items(), 1):
        f = "[FREE]" if p["free"] else "[PAID]"
        print(f"  |  {i:>2}. {p['name']:<32} {f} {len(p['models']):>2}m |")
    print("  +==================================================+")


def show_models(pk):
    p = PROVIDERS[pk]
    print(f"\n  {p['name']} - {p['info']}")
    for m in p["models"]:
        ctx = f"{m['cw']//1000}K" if m['cw'] < 1000000 else f"{m['cw']//1000000}M"
        r = "[R] " if m.get("r") else "    "
        print(f"    {r}{m['name']:<38} {ctx}")


def show_done(pk, key_entries, aliases, device_rotated=False):
    p = PROVIDERS[pk]
    pf = load_json(AUTH_PROFILES)
    dev_note = "device rotated" if device_rotated else "device unchanged"
    print(f"\n  +==================================================+")
    print(f"  |  [OK] Setup Complete                              |")
    print(f"  +==================================================+")
    print(f"  |  {p['name']:<48}|")
    print(f"  |  {len(key_entries)} keys * {len(p['models'])} models * {dev_note:<17}|")
    print(f"  +==================================================+")

    # Show bucket distribution
    buckets = {}
    for e in key_entries:
        buckets.setdefault(e["bucket"], 0)
        buckets[e["bucket"]] += 1
    if len(buckets) > 1 or list(buckets.keys()) != ["default"]:
        for b, count in sorted(buckets.items()):
            print(f"  |  bucket={b}: {count} key(s){' ' * (35 - len(b) - len(str(count)))}|")
        print(f"  +==================================================+")

    for a in aliases[:10]:
        k = pf.get("profiles", {}).get(a, {}).get("key", "?")
        t = f"...{k[-8:]}" if len(k) > 8 else k
        b = pf.get("profiles", {}).get(a, {}).get("bucket", "default")
        btag = f" [{b}]" if b != "default" else ""
        line = f"{a}{btag}"
        print(f"  |  {line:<22} {t:<26}|")
    if len(aliases) > 10:
        print(f"  |  ... and {len(aliases) - 10} more{' ' * 35}|")
    print(f"  +==================================================+")
    fm = f"{pk}/{p['models'][0]['id']}"
    if len(fm) > 44:
        fm = fm[:41] + "..."
    print(f"  |  /model {fm:<40}|")
    print(f"  +==================================================+")


def show_help():
    print("""
  +==================================================+
  |  OpenClaw Key Manager v4.0                       |
  +==================================================+
  |                                                  |
  |  Usage:                                          |
  |    python3 openclaw_key_manage.py                |
  |    python3 openclaw_key_manage.py --status        |
  |    python3 openclaw_key_manage.py --fix           |
  |    python3 openclaw_key_manage.py --remove NAME   |
  |    python3 openclaw_key_manage.py --rotate-device |
  |    python3 openclaw_key_manage.py --help          |
  |                                                  |
  |  Commands:                                       |
  |    (no args)       Interactive provider setup     |
  |    --status        Show all keys and providers   |
  |    --fix           Repair broken v3.x configs    |
  |    --remove        Remove a provider cleanly     |
  |    --rotate-device Also rotate device identity   |
  |    --no-restart    Skip gateway restart           |
  |                                                  |
  |  keys.txt format:                                |
  |    AIzaSy...                                     |
  |    AIzaSy... # bucket=projA                      |
  |    AIzaSy... # bucket=projB                      |
  |                                                  |
  |  Bucket tags group keys by Google project for    |
  |  project-level cooldown rotation.                |
  |                                                  |
  +==================================================+
""")


# ===================================================================
#  MAIN
# ===================================================================

def main():
    args = sys.argv[1:]

    if "--help" in args or "-h" in args:
        show_help()
        return

    if "--fix" in args:
        fix_config()
        return

    if "--status" in args:
        show_status()
        return

    if "--remove" in args:
        idx = args.index("--remove")
        if idx + 1 >= len(args):
            print("  Usage: --remove PROVIDER_NAME")
            print(f"  Available: {', '.join(PROVIDERS.keys())}")
            return
        pk = args[idx + 1].lower()
        if pk not in PROVIDERS:
            matches = [k for k in PROVIDERS if pk in k]
            if len(matches) == 1:
                pk = matches[0]
            else:
                print(f"  Unknown provider: {pk}")
                print(f"  Available: {', '.join(PROVIDERS.keys())}")
                return
        confirm = input(f"  Remove {pk} from all configs? (y/n): ").strip().lower()
        if confirm == 'y':
            remove_provider(pk)
        return

    rotate_device = "--rotate-device" in args
    no_restart = "--no-restart" in args

    # -- Interactive setup --
    print("\n  +==================================================+")
    print("  |  OpenClaw Key Manager v4.0                       |")
    print("  |  Keys * Models * Bucket Rotation                 |")
    print("  +==================================================+")

    preflight()
    show_providers()
    pkeys = list(PROVIDERS.keys())

    while True:
        try:
            c = input(f"\n  Provider (1-{len(pkeys)}, name, or 'q' to quit): ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\n  Cancelled.")
            return

        if c in ('q', 'quit', 'exit'):
            return
        if c.isdigit() and 1 <= int(c) <= len(pkeys):
            pk = pkeys[int(c) - 1]
            break
        elif c in PROVIDERS:
            pk = c
            break
        else:
            matches = [k for k in pkeys if c in k]
            if len(matches) == 1:
                pk = matches[0]
                break
            elif len(matches) > 1:
                print(f"  Multiple matches: {', '.join(matches)}")
            else:
                print("  Try again.")

    show_models(pk)

    # Read keys (now returns structured entries with bucket)
    key_entries = read_keys()
    keys_only = [e["key"] for e in key_entries]
    buckets = set(e["bucket"] for e in key_entries)
    print(f"\n  Found {len(key_entries)} key(s) in {KEYS_FILE}")
    if len(buckets) > 1 or list(buckets) != ["default"]:
        print(f"  Buckets: {', '.join(sorted(buckets))}")

    # Validate key prefixes
    p = PROVIDERS[pk]
    if p["prefix"]:
        bad = [e for e in key_entries if not e["key"].startswith(p["prefix"])]
        if bad:
            print(f"\n  [!!] {len(bad)} key(s) don't start with '{p['prefix']}'")
            for b in bad[:3]:
                print(f"     -> {b['key'][:20]}...")
            if len(bad) > 3:
                print(f"     ... and {len(bad) - 3} more")
            try:
                ans = input("  Continue anyway? (y/n): ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                print("\n  Cancelled.")
                return
            if ans != 'y':
                return

    print(f"\n  {'=' * 50}")
    print(f"  Setting up: {p['name']}")
    print(f"  Keys: {len(key_entries)} * Models: {len(p['models'])} * Buckets: {len(buckets)}")
    if rotate_device:
        print(f"  Device rotation: YES")
    print(f"  {'=' * 50}")

    # Run setup steps
    aliases = step_auth_profiles(pk, key_entries)
    if not aliases:
        return
    step_auth_json(pk, keys_only[0])
    step_models_json(pk)
    step_openclaw_json(pk, keys_only[0])

    if rotate_device:
        step_device()

    show_done(pk, key_entries, aliases, device_rotated=rotate_device)

    # Restart gateway (unless --no-restart)
    if no_restart:
        print("\n  Skipping gateway restart (--no-restart)")
        print("  -> Restart manually when ready: openclaw gateway restart")
    else:
        print("\n  Restarting gateway...")
        try:
            r = subprocess.run(
                ["openclaw", "gateway", "restart"],
                capture_output=True, text=True, timeout=30
            )
            if r.returncode == 0:
                print("  [OK] Gateway restarted")
            else:
                print(f"  [!!] Gateway returned code {r.returncode}")
                if r.stderr:
                    print(f"       {r.stderr.strip()[:100]}")
                print("  -> Try manually: openclaw gateway restart")
        except FileNotFoundError:
            print("  [!!] 'openclaw' not found in PATH")
            print("  -> Restart manually: openclaw gateway restart")
        except subprocess.TimeoutExpired:
            print("  [!!] Gateway restart timed out (30s)")
            print("  -> Try manually: openclaw gateway restart")
        except Exception as e:
            print(f"  [!!] {e}")
            print("  -> Try manually: openclaw gateway restart")

    print()


if __name__ == "__main__":
    main()
