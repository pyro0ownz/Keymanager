#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║  OpenClaw Key Manager v3.2                                      ║
║  Multi-Provider Key Pool + Device Identity Management           ║
║  Built for OpenClaw 2026.2.24+                                  ║
╚══════════════════════════════════════════════════════════════════╝

Files touched:
  ~/.openclaw/agents/main/agent/auth-profiles.json   (key pool + stats)
  ~/.openclaw/agents/main/agent/auth.json             (active key)
  ~/.openclaw/agents/main/agent/models.json           (provider defs)
  ~/.openclaw/openclaw.json                           (env, auth, models, whitelist)
  ~/.openclaw/device.json                             (Ed25519 identity)

Changelog v3.2 (from actual working openclaw.json analysis):
  - FIXED: Google is now handled as NATIVE provider — does NOT inject into
    models.providers (OpenClaw manages Google natively via auth profiles).
    v3.0 was injecting "api": "google" which is invalid and broke the config.
  - FIXED: Google setup only touches env, auth.profiles, and the whitelist.
    models.providers is left alone for Google. Other providers (nvidia-nim,
    groq, etc.) still use models.providers with "api": "openai-completions".
  - FIXED: --fix now removes the invalid google entry from models.providers
  - ADDED: --fix flag to repair broken configs from v3.0
  - ADDED: --status to show current key pool state
  - ADDED: --remove to cleanly remove a provider
  - ADDED: Better error handling and validation throughout
  - ADDED: Pre-flight checks before writing any files
  - IMPROVED: Key prefix validation with clearer messaging
  - IMPROVED: Backup naming with ISO timestamps
"""

import json, os, sys, shutil, subprocess, hashlib, secrets, time
from datetime import datetime
from pathlib import Path

# ═══════════════════════════════════════════════════════════════════
#  Ed25519 KEY GENERATION (multi-method fallback)
# ═══════════════════════════════════════════════════════════════════

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

    print("\n  ✗ Cannot generate Ed25519 keys. Tried:")
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


# ═══════════════════════════════════════════════════════════════════
#  PROVIDER CATALOG
#
#  api types: "google-genai" for native Google, "openai-completions"
#  for OpenAI-compatible endpoints.
#
#  url: Set to None for providers that don't need baseUrl (Google).
#       OpenClaw validates baseUrl as a required string IF present,
#       so we must OMIT it entirely for Google, not set it to null.
# ═══════════════════════════════════════════════════════════════════

PROVIDERS = {
    "google": {
        "name": "Google Gemini (AI Studio)",
        "api": "native",  # Google is handled NATIVELY by OpenClaw — NOT through models.providers
        "url": None,
        "prefix": "AIzaSy",
        "free": True,
        "native": True,  # Flag: skip models.providers injection, use auth profiles only
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

# ═══════════════════════════════════════════════════════════════════
#  PATHS
# ═══════════════════════════════════════════════════════════════════

OPENCLAW_DIR   = os.path.expanduser("~/.openclaw")
AGENT_DIR      = os.path.join(OPENCLAW_DIR, "agents/main/agent")
AUTH_PROFILES  = os.path.join(AGENT_DIR, "auth-profiles.json")
AUTH_JSON      = os.path.join(AGENT_DIR, "auth.json")
MODELS_JSON    = os.path.join(AGENT_DIR, "models.json")
OPENCLAW_JSON  = os.path.join(OPENCLAW_DIR, "openclaw.json")
DEVICE_JSON    = os.path.join(OPENCLAW_DIR, "device.json")
KEYS_FILE      = "keys.txt"

# ═══════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════

def load_json(path):
    """Load JSON file, return empty dict if missing or corrupt."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        print(f"  ⚠  {os.path.basename(path)} has invalid JSON: {e}")
        print(f"     Backup will be created before overwriting.")
        return {}
    except PermissionError:
        print(f"  ✗  Cannot read {path} — permission denied")
        sys.exit(1)


def save_json(path, data):
    """Save JSON with timestamped backup of existing file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = f"{path}.bak.{ts}"
        shutil.copy2(path, backup)
    with open(path, 'w') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"    ✓ {os.path.basename(path)}")


def read_keys(keys_file=None):
    """Read API keys from file, one per line. Lines starting with # are comments."""
    kf = keys_file or KEYS_FILE
    if not os.path.exists(kf):
        print(f"\n  ✗ {kf} not found")
        print(f"    Create it with one API key per line:")
        print(f"    echo 'AIzaSy...' > {kf}")
        sys.exit(1)
    keys = []
    with open(kf) as f:
        for line_num, line in enumerate(f, 1):
            stripped = line.strip()
            if not stripped or stripped.startswith('#'):
                continue
            # Strip inline comments
            if ' #' in stripped:
                stripped = stripped.split(' #')[0].strip()
            if stripped:
                keys.append(stripped)
    if not keys:
        print(f"\n  ✗ {kf} has no keys (only blank lines/comments)")
        sys.exit(1)
    return keys


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
    """Build a provider config entry. Omits baseUrl for providers that don't use it."""
    p = PROVIDERS[pk]
    entry = {
        "api": p["api"],
        "models": [model_schema(m) for m in p["models"]],
        "apiKey": p["env"],
    }
    # CRITICAL: Only include baseUrl if the provider has one.
    # Google uses native API — no baseUrl. OpenClaw validates baseUrl
    # as a required string IF the key exists, so we must OMIT it entirely.
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
    # Same rule: omit baseUrl entirely for Google
    if p["url"] is not None:
        entry["baseUrl"] = p["url"]
    return entry


# ═══════════════════════════════════════════════════════════════════
#  PRE-FLIGHT CHECKS
# ═══════════════════════════════════════════════════════════════════

def preflight():
    """Verify OpenClaw installation exists before touching anything."""
    issues = []
    if not os.path.isdir(OPENCLAW_DIR):
        issues.append(f"~/.openclaw directory not found")
    if not os.path.isdir(AGENT_DIR):
        issues.append(f"Agent directory not found: {AGENT_DIR}")
    if not os.path.exists(OPENCLAW_JSON):
        issues.append(f"openclaw.json not found")

    if issues:
        print("\n  ✗ Pre-flight check failed:")
        for i in issues:
            print(f"    - {i}")
        print("\n  Is OpenClaw installed? Run: openclaw --version")
        sys.exit(1)

    # Verify openclaw.json is valid JSON
    try:
        with open(OPENCLAW_JSON) as f:
            json.load(f)
    except json.JSONDecodeError as e:
        print(f"\n  ✗ openclaw.json is corrupt: {e}")
        print(f"    Run: openclaw doctor --fix")
        sys.exit(1)

    return True


# ═══════════════════════════════════════════════════════════════════
#  CONFIG REPAIR (--fix)
# ═══════════════════════════════════════════════════════════════════

def fix_config():
    """Repair known v3.0 issues in openclaw.json and models.json."""
    print("\n  ╔══════════════════════════════════════════════════╗")
    print("  ║  Config Repair                                    ║")
    print("  ╚══════════════════════════════════════════════════╝")

    fixed = 0

    # Fix openclaw.json
    c = load_json(OPENCLAW_JSON)
    if c:
        providers = c.get("models", {}).get("providers", {})

        # Fix 1: Remove Google from models.providers entirely
        # OpenClaw handles Google natively via auth profiles.
        # v3.0 injected it with "api": "google" which is invalid.
        if "google" in providers:
            del providers["google"]
            print("  ✓ Removed 'google' from models.providers (native provider)")
            print("    Google is handled through auth profiles, not models.providers")
            fixed += 1

        # Fix 2: Remove null/undefined baseUrl from any provider
        for pk, prov in list(providers.items()):
            if "baseUrl" in prov and (prov["baseUrl"] is None or prov["baseUrl"] == ""):
                del prov["baseUrl"]
                print(f"  ✓ Removed empty baseUrl from {pk}")
                fixed += 1

        if fixed > 0:
            save_json(OPENCLAW_JSON, c)

    # Fix models.json
    m = load_json(MODELS_JSON)
    if m:
        m_fixed = 0
        m_providers = m.get("providers", {})

        # Same fix: remove Google from models.json providers
        if "google" in m_providers:
            del m_providers["google"]
            print("  ✓ Removed 'google' from models.json providers")
            m_fixed += 1

        for pk, prov in list(m_providers.items()):
            if "baseUrl" in prov and (prov["baseUrl"] is None or prov["baseUrl"] == ""):
                del prov["baseUrl"]
                print(f"  ✓ Removed empty baseUrl from models.json/{pk}")
                m_fixed += 1

        if m_fixed > 0:
            save_json(MODELS_JSON, m)
            fixed += m_fixed

    if fixed == 0:
        print("\n  ✓ No issues found — config looks clean")
    else:
        print(f"\n  ✓ Fixed {fixed} issue(s)")
        print("  → Run: openclaw doctor")
        print("  → Then: openclaw gateway restart")

    return fixed


# ═══════════════════════════════════════════════════════════════════
#  STATUS DISPLAY
# ═══════════════════════════════════════════════════════════════════

def show_status():
    """Display current state of all configured providers and keys."""
    print("\n  ╔══════════════════════════════════════════════════╗")
    print("  ║  Key Pool Status                                  ║")
    print("  ╚══════════════════════════════════════════════════╝")

    profiles = load_json(AUTH_PROFILES)
    auth = load_json(AUTH_JSON)
    models = load_json(MODELS_JSON)

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

    for prov, aliases in sorted(by_provider.items()):
        active_key = auth.get(prov, {}).get("key", "")
        model_count = len(models.get("providers", {}).get(prov, {}).get("models", []))
        print(f"\n  ── {prov} ({len(aliases)} keys, {model_count} models) ──")

        for alias in sorted(aliases):
            key = profiles["profiles"][alias].get("key", "???")
            s = stats.get(alias, {})
            errs = s.get("errorCount", 0)
            last_fail = s.get("lastFailureAt", 0)

            # Status icon
            if errs == 0:
                icon = "✅"
            elif errs <= 3:
                icon = "🟡"
            else:
                icon = "⚠️ "

            # Active marker
            active = " ◄ ACTIVE" if key == active_key else ""

            # Cooldown check
            cooldown = ""
            if last_fail > 0:
                elapsed = time.time() - (last_fail / 1000 if last_fail > 1e10 else last_fail)
                if elapsed < 60:
                    cooldown = f" (cooling {60-int(elapsed)}s)"

            print(f"    {icon} {alias:<25} ...{key[-8:]}  "
                  f"err:{errs}{cooldown}{active}")

        if prov in last_good:
            print(f"    Last good: {last_good[prov]}")


# ═══════════════════════════════════════════════════════════════════
#  PROVIDER REMOVAL
# ═══════════════════════════════════════════════════════════════════

def remove_provider(pk):
    """Cleanly remove a provider from all config files."""
    print(f"\n  Removing {pk} from all configs...")
    removed = 0

    # auth-profiles.json
    ap = load_json(AUTH_PROFILES)
    for key in list(ap.get("profiles", {}).keys()):
        if key.startswith(f"{pk}:"):
            del ap["profiles"][key]
            if key in ap.get("usageStats", {}):
                del ap["usageStats"][key]
            removed += 1
    ap.get("lastGood", {}).pop(pk, None)
    if removed:
        save_json(AUTH_PROFILES, ap)

    # auth.json
    auth = load_json(AUTH_JSON)
    if pk in auth:
        del auth[pk]
        save_json(AUTH_JSON, auth)
        removed += 1

    # models.json
    models = load_json(MODELS_JSON)
    if pk in models.get("providers", {}):
        del models["providers"][pk]
        save_json(MODELS_JSON, models)
        removed += 1

    # openclaw.json
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

    print(f"\n  ✓ Removed {pk} ({removed} entries cleaned)")
    print("  → Run: openclaw gateway restart")


# ═══════════════════════════════════════════════════════════════════
#  THE FIVE SETUP STEPS
# ═══════════════════════════════════════════════════════════════════

def step_auth_profiles(pk, keys):
    """Step 1: auth-profiles.json — build/merge key pool with usage stats."""
    d = load_json(AUTH_PROFILES)
    d.setdefault("version", 1)
    d.setdefault("profiles", {})
    d.setdefault("lastGood", {})
    d.setdefault("usageStats", {})

    # Find existing key count for this provider to avoid overwriting
    existing = [k for k in d["profiles"] if k.startswith(f"{pk}:")]
    start_idx = len(existing) + 1

    aliases = []
    for i, key in enumerate(keys):
        # Check if this key already exists
        dupe = False
        for alias, info in d["profiles"].items():
            if info.get("key") == key:
                aliases.append(alias)
                dupe = True
                break
        if dupe:
            continue

        idx = start_idx + len(aliases) - len([a for a in aliases if a.startswith(f"{pk}:")])
        # Recalculate: just use incrementing numbers
        a = f"{pk}:key{start_idx + i}"
        # Make sure alias doesn't collide
        while a in d["profiles"]:
            start_idx += 1
            a = f"{pk}:key{start_idx + i}"

        aliases.append(a)
        d["profiles"][a] = {"type": "api_key", "provider": pk, "key": key}
        d["usageStats"].setdefault(a, {
            "lastUsed": 0, "errorCount": 0, "lastFailureAt": 0
        })

    # Include existing aliases in the return
    all_aliases = existing + [a for a in aliases if a not in existing]
    d["lastGood"][pk] = all_aliases[0] if all_aliases else aliases[0]

    new_count = len(aliases)
    total = len(all_aliases)
    print(f"\n  [1/5] auth-profiles.json — {new_count} new, {total} total")
    save_json(AUTH_PROFILES, d)
    return all_aliases


def step_auth_json(pk, key):
    """Step 2: auth.json — set active key for provider."""
    d = load_json(AUTH_JSON)
    d[pk] = {"type": "api_key", "key": key}
    print(f"\n  [2/5] auth.json — active: ...{key[-8:]}")
    save_json(AUTH_JSON, d)


def step_models_json(pk):
    """Step 3: models.json — register provider and model definitions.
    SKIPPED for native providers (Google) — OpenClaw handles those internally."""
    p = PROVIDERS[pk]
    if p.get("native"):
        print(f"\n  [3/5] models.json — skipped (native provider)")
        return
    d = load_json(MODELS_JSON)
    d.setdefault("providers", {})
    d["providers"][pk] = build_provider_entry(pk)
    print(f"\n  [3/5] models.json — {len(p['models'])} models")
    save_json(MODELS_JSON, d)


def step_openclaw_json(pk, key):
    """Step 4: openclaw.json — env vars, auth profiles, models, whitelist.
    For native providers (Google): only sets env, auth profile, and whitelist.
    Does NOT inject into models.providers — OpenClaw handles native providers internally."""
    p = PROVIDERS[pk]
    c = load_json(OPENCLAW_JSON)
    if not c:
        print("  ✗ Cannot read openclaw.json")
        sys.exit(1)

    # env section — always set
    c.setdefault("env", {})[p["env"]] = key

    # auth.profiles — always register
    c.setdefault("auth", {}).setdefault("profiles", {})
    c["auth"]["profiles"][f"{pk}:default"] = {"provider": pk, "mode": "api_key"}

    # models.providers — ONLY for non-native providers
    # Google is handled natively by OpenClaw. Injecting it into models.providers
    # with "api": "google" breaks validation. OpenClaw expects Google to be
    # configured through auth profiles only.
    if not p.get("native"):
        c.setdefault("models", {}).setdefault("providers", {})
        c["models"]["providers"][pk] = build_provider_entry_with_envref(pk, key)

    # agents.defaults.models — whitelist (always)
    wl = c.setdefault("agents", {}).setdefault("defaults", {}).setdefault("models", {})
    for m in p["models"]:
        wl[f"{pk}/{m['id']}"] = {}

    print(f"\n  [4/5] openclaw.json — env + auth + {'models + ' if not p.get('native') else ''}whitelist")
    save_json(OPENCLAW_JSON, c)


def step_device():
    """Step 5: device.json — generate fresh Ed25519 device identity."""
    dev = generate_device()
    print(f"\n  [5/5] device.json — new identity")
    save_json(DEVICE_JSON, dev)
    print(f"    ID: {dev['deviceId'][:16]}...")
    return dev


# ═══════════════════════════════════════════════════════════════════
#  DISPLAY HELPERS
# ═══════════════════════════════════════════════════════════════════

def show_providers():
    print("\n  ╔══════════════════════════════════════════════════╗")
    print("  ║  Providers                                       ║")
    print("  ╠══════════════════════════════════════════════════╣")
    for i, (k, p) in enumerate(PROVIDERS.items(), 1):
        f = "🟢" if p["free"] else "💰"
        print(f"  ║  {i:>2}. {p['name']:<32} {f} {len(p['models']):>2}m ║")
    print("  ╚══════════════════════════════════════════════════╝")


def show_models(pk):
    p = PROVIDERS[pk]
    print(f"\n  {p['name']} — {p['info']}")
    for m in p["models"]:
        ctx = f"{m['cw']//1000}K" if m['cw'] < 1000000 else f"{m['cw']//1000000}M"
        r = "🧠" if m.get("r") else "  "
        print(f"    {r} {m['name']:<38} {ctx}")


def show_done(pk, keys, aliases):
    p = PROVIDERS[pk]
    pf = load_json(AUTH_PROFILES)
    print(f"\n  ╔══════════════════════════════════════════════════╗")
    print(f"  ║  ✅ Setup Complete                                ║")
    print(f"  ╠══════════════════════════════════════════════════╣")
    print(f"  ║  {p['name']:<48} ║")
    print(f"  ║  {len(keys)} keys • {len(p['models'])} models • device rotated       ║")
    print(f"  ╠══════════════════════════════════════════════════╣")
    for a in aliases[:10]:  # Show max 10
        k = pf.get("profiles", {}).get(a, {}).get("key", "?")
        t = f"...{k[-8:]}" if len(k) > 8 else k
        print(f"  ║  {a:<20} {t:<28} ║")
    if len(aliases) > 10:
        print(f"  ║  ... and {len(aliases)-10} more{' '*33}║")
    print(f"  ╠══════════════════════════════════════════════════╣")
    fm = f"{pk}/{p['models'][0]['id']}"
    if len(fm) > 44:
        fm = fm[:41] + "..."
    print(f"  ║  /model {fm:<40} ║")
    print(f"  ╚══════════════════════════════════════════════════╝")


def show_help():
    print("""
  ╔══════════════════════════════════════════════════╗
  ║  OpenClaw Key Manager v3.2                       ║
  ╠══════════════════════════════════════════════════╣
  ║                                                  ║
  ║  Usage:                                          ║
  ║    python3 openclaw_key_manage.py                ║
  ║    python3 openclaw_key_manage.py --status        ║
  ║    python3 openclaw_key_manage.py --fix           ║
  ║    python3 openclaw_key_manage.py --remove NAME   ║
  ║    python3 openclaw_key_manage.py --help          ║
  ║                                                  ║
  ║  Commands:                                       ║
  ║    (no args)    Interactive provider setup        ║
  ║    --status     Show all keys and providers      ║
  ║    --fix        Repair broken v3.0 configs       ║
  ║    --remove     Remove a provider cleanly        ║
  ║                                                  ║
  ║  Setup:                                          ║
  ║    1. Put API keys in keys.txt (one per line)    ║
  ║    2. Run this script                            ║
  ║    3. Pick your provider                         ║
  ║    4. Done — gateway restarts automatically      ║
  ║                                                  ║
  ║  Run once per provider. Keys merge, nothing      ║
  ║  gets overwritten.                               ║
  ║                                                  ║
  ╚══════════════════════════════════════════════════╝
""")


# ═══════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════

def main():
    # Handle CLI flags
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
            # Check if it's a partial match
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

    # ── Interactive setup ──
    print("\n  ╔══════════════════════════════════════════════════╗")
    print("  ║  OpenClaw Key Manager v3.2                       ║")
    print("  ║  Keys • Models • Device Identity                 ║")
    print("  ╚══════════════════════════════════════════════════╝")

    # Pre-flight
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

    # Read keys
    keys = read_keys()
    print(f"\n  Found {len(keys)} key(s) in {KEYS_FILE}")

    # Validate key prefixes
    p = PROVIDERS[pk]
    if p["prefix"]:
        bad = [k for k in keys if not k.startswith(p["prefix"])]
        if bad:
            print(f"\n  ⚠  {len(bad)} key(s) don't start with '{p['prefix']}'")
            for b in bad[:3]:
                print(f"     → {b[:20]}...")
            if len(bad) > 3:
                print(f"     ... and {len(bad)-3} more")
            try:
                ans = input("  Continue anyway? (y/n): ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                print("\n  Cancelled.")
                return
            if ans != 'y':
                return

    # Confirm
    print(f"\n  {'═'*50}")
    print(f"  Setting up: {p['name']}")
    print(f"  Keys: {len(keys)} • Models: {len(p['models'])}")
    print(f"  {'═'*50}")

    # Run the five steps
    aliases = step_auth_profiles(pk, keys)
    step_auth_json(pk, keys[0])
    step_models_json(pk)
    step_openclaw_json(pk, keys[0])
    step_device()

    show_done(pk, keys, aliases)

    # Restart gateway
    print("\n  Restarting gateway...")
    try:
        r = subprocess.run(
            ["openclaw", "gateway", "restart"],
            capture_output=True, text=True, timeout=30
        )
        if r.returncode == 0:
            print("  ✅ Gateway restarted")
        else:
            print(f"  ⚠  Gateway returned code {r.returncode}")
            if r.stderr:
                print(f"     {r.stderr.strip()[:100]}")
            print("  → Try manually: openclaw gateway restart")
    except FileNotFoundError:
        print("  ⚠  'openclaw' not found in PATH")
        print("  → Restart manually: openclaw gateway restart")
    except subprocess.TimeoutExpired:
        print("  ⚠  Gateway restart timed out (30s)")
        print("  → Try manually: openclaw gateway restart")
    except Exception as e:
        print(f"  ⚠  {e}")
        print("  → Try manually: openclaw gateway restart")

    print()


if __name__ == "__main__":
    main()
