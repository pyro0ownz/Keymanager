#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║  OpenClaw Key Manager v3.0                                      ║
║  Key Rotation + Device Identity Rotation                        ║
║  Built from actual OpenClaw 2026.2.24 file structures           ║
╚══════════════════════════════════════════════════════════════════╝

Files touched:
  ~/.openclaw/agents/main/agent/auth-profiles.json   (key pool + stats)
  ~/.openclaw/agents/main/agent/auth.json             (active key)
  ~/.openclaw/agents/main/agent/models.json           (provider defs)
  ~/.openclaw/openclaw.json                           (env, auth, models, whitelist)
  ~/.openclaw/device.json                             (Ed25519 identity — rotated)
"""

import json, os, sys, shutil, subprocess, hashlib, secrets, time, struct
from datetime import datetime

# ═══════════════════════════════════════════════════════════════════
#  PURE PYTHON Ed25519 — no dependencies needed
# ═══════════════════════════════════════════════════════════════════

def _ed25519_generate():
    """Generate Ed25519 keypair. Tries cryptography lib first, falls back to openssl CLI."""
    # Try 1: cryptography library
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, PublicFormat, NoEncryption
        priv = Ed25519PrivateKey.generate()
        pub_pem = priv.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
        priv_pem = priv.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
        return pub_pem, priv_pem
    except ImportError:
        pass

    # Try 2: openssl CLI
    try:
        r = subprocess.run(["openssl", "genpkey", "-algorithm", "Ed25519", "-outform", "PEM"],
                          capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and "BEGIN PRIVATE KEY" in r.stdout:
            priv_pem = r.stdout
            r2 = subprocess.run(["openssl", "pkey", "-pubout"],
                               input=priv_pem, capture_output=True, text=True, timeout=10)
            if r2.returncode == 0:
                return r2.stdout, priv_pem
    except:
        pass

    # Try 3: PyNaCl
    try:
        import nacl.signing, base64
        sk = nacl.signing.SigningKey.generate()
        vk = sk.verify_key
        # Wrap in PEM format
        priv_raw = bytes(sk) + bytes(vk)
        pub_raw = bytes(vk)
        priv_pem = "-----BEGIN PRIVATE KEY-----\n" + base64.b64encode(
            b'\x30\x2e\x02\x01\x00\x30\x05\x06\x03\x2b\x65\x70\x04\x22\x04\x20' + bytes(sk)
        ).decode() + "\n-----END PRIVATE KEY-----\n"
        pub_pem = "-----BEGIN PUBLIC KEY-----\n" + base64.b64encode(
            b'\x30\x2a\x30\x05\x06\x03\x2b\x65\x70\x03\x21\x00' + pub_raw
        ).decode() + "\n-----END PUBLIC KEY-----\n"
        return pub_pem, priv_pem
    except ImportError:
        pass

    print("  ERROR: Need 'cryptography' or 'openssl' for Ed25519 key generation.")
    print("  Install: pip install cryptography --break-system-packages")
    sys.exit(1)


def generate_device():
    """Generate a new device.json matching OpenClaw's exact format."""
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
# ═══════════════════════════════════════════════════════════════════

PROVIDERS = {
    "google": {
        "name": "Google Gemini (AI Studio)",
        "api": "google", "url": None,
        "prefix": "AIzaSy", "free": True,
        "info": "15 RPM, 1M TPD free | ai.google.dev",
        "env": "GOOGLE_API_KEY",
        "models": [
            {"id": "gemini-2.0-flash", "name": "Gemini 2.0 Flash", "cw": 1048576, "mt": 8192, "r": False},
            {"id": "gemini-2.5-flash", "name": "Gemini 2.5 Flash", "cw": 1048576, "mt": 65536, "r": True},
            {"id": "gemini-2.5-flash-lite", "name": "Gemini 2.5 Flash Lite", "cw": 1048576, "mt": 65536, "r": False},
            {"id": "gemini-3-flash-preview", "name": "Gemini 3 Flash Preview", "cw": 1048576, "mt": 65536, "r": True},
            {"id": "gemini-flash-latest", "name": "Gemini Flash Latest", "cw": 1048576, "mt": 8192, "r": False},
            {"id": "gemini-flash-lite-latest", "name": "Gemini Flash Lite Latest", "cw": 1048576, "mt": 8192, "r": False},
        ]
    },
    "groq": {
        "name": "Groq",
        "api": "openai-completions", "url": "https://api.groq.com/openai/v1",
        "prefix": "gsk_", "free": True,
        "info": "30 RPM free | console.groq.com",
        "env": "GROQ_API_KEY",
        "models": [
            {"id": "llama-3.3-70b-versatile", "name": "LLaMA 3.3 70B", "cw": 128000, "mt": 32768, "r": False},
            {"id": "llama-3.1-8b-instant", "name": "LLaMA 3.1 8B", "cw": 128000, "mt": 8192, "r": False},
            {"id": "gemma2-9b-it", "name": "Gemma 2 9B", "cw": 8192, "mt": 8192, "r": False},
            {"id": "mixtral-8x7b-32768", "name": "Mixtral 8x7B", "cw": 32768, "mt": 32768, "r": False},
            {"id": "deepseek-r1-distill-llama-70b", "name": "DeepSeek R1 Distill 70B", "cw": 128000, "mt": 16384, "r": True},
            {"id": "qwen-qwq-32b", "name": "Qwen QWQ 32B", "cw": 128000, "mt": 16384, "r": True},
        ]
    },
    "nvidia-nim": {
        "name": "NVIDIA NIM",
        "api": "openai-completions", "url": "https://integrate.api.nvidia.com/v1",
        "prefix": "nvapi-", "free": True,
        "info": "1000 req/day free | build.nvidia.com",
        "env": "NVIDIA_API_KEY",
        "models": [
            {"id": "moonshotai/kimi-k2.5", "name": "Kimi K2.5", "cw": 200000, "mt": 8192, "r": False},
            {"id": "nvidia/llama-3.1-nemotron-70b-instruct", "name": "Nemotron 70B", "cw": 131072, "mt": 4096, "r": False},
            {"id": "meta/llama-3.3-70b-instruct", "name": "Meta LLaMA 3.3 70B", "cw": 131072, "mt": 4096, "r": False},
            {"id": "nvidia/llama-3.1-405b-instruct", "name": "LLaMA 3.1 405B", "cw": 128000, "mt": 4096, "r": False},
            {"id": "nvidia/mistral-nemo-minitron-8b-8k-instruct", "name": "Mistral NeMo 8B", "cw": 8192, "mt": 2048, "r": False},
            {"id": "deepseek-ai/deepseek-r1", "name": "DeepSeek R1", "cw": 64000, "mt": 8192, "r": True},
            {"id": "mistralai/mistral-large-2-instruct", "name": "Mistral Large 2", "cw": 128000, "mt": 4096, "r": False},
            {"id": "qwen/qwen2.5-72b-instruct", "name": "Qwen 2.5 72B", "cw": 128000, "mt": 4096, "r": False},
        ]
    },
    "openrouter": {
        "name": "OpenRouter",
        "api": "openai-completions", "url": "https://openrouter.ai/api/v1",
        "prefix": "sk-or-", "free": True,
        "info": "Free models | openrouter.ai",
        "env": "OPENROUTER_API_KEY",
        "models": [
            {"id": "google/gemini-2.0-flash-exp:free", "name": "Gemini 2.0 Flash (Free)", "cw": 1048576, "mt": 8192, "r": False},
            {"id": "deepseek/deepseek-r1:free", "name": "DeepSeek R1 (Free)", "cw": 164000, "mt": 16384, "r": True},
            {"id": "meta-llama/llama-3.3-70b-instruct:free", "name": "LLaMA 3.3 70B (Free)", "cw": 128000, "mt": 8192, "r": False},
            {"id": "qwen/qwen3-235b-a22b:free", "name": "Qwen 3 235B (Free)", "cw": 40960, "mt": 8192, "r": True},
        ]
    },
    "mistral": {
        "name": "Mistral AI",
        "api": "openai-completions", "url": "https://api.mistral.ai/v1",
        "prefix": "", "free": True,
        "info": "Free tier | console.mistral.ai",
        "env": "MISTRAL_API_KEY",
        "models": [
            {"id": "mistral-large-latest", "name": "Mistral Large", "cw": 128000, "mt": 8192, "r": False},
            {"id": "codestral-latest", "name": "Codestral", "cw": 256000, "mt": 8192, "r": False},
            {"id": "open-mistral-nemo", "name": "Mistral Nemo", "cw": 128000, "mt": 8192, "r": False},
        ]
    },
    "together": {
        "name": "Together AI",
        "api": "openai-completions", "url": "https://api.together.xyz/v1",
        "prefix": "", "free": True,
        "info": "$5 free | api.together.ai",
        "env": "TOGETHER_API_KEY",
        "models": [
            {"id": "meta-llama/Llama-3.3-70B-Instruct-Turbo", "name": "LLaMA 3.3 70B Turbo", "cw": 128000, "mt": 8192, "r": False},
            {"id": "deepseek-ai/DeepSeek-R1", "name": "DeepSeek R1", "cw": 164000, "mt": 16384, "r": True},
            {"id": "Qwen/Qwen2.5-72B-Instruct-Turbo", "name": "Qwen 2.5 72B", "cw": 128000, "mt": 8192, "r": False},
        ]
    },
    "cerebras": {
        "name": "Cerebras",
        "api": "openai-completions", "url": "https://api.cerebras.ai/v1",
        "prefix": "csk-", "free": True,
        "info": "30 RPM free | cloud.cerebras.ai",
        "env": "CEREBRAS_API_KEY",
        "models": [
            {"id": "llama-3.3-70b", "name": "LLaMA 3.3 70B", "cw": 128000, "mt": 8192, "r": False},
            {"id": "deepseek-r1-distill-llama-70b", "name": "DeepSeek R1 Distill 70B", "cw": 128000, "mt": 16384, "r": True},
        ]
    },
    "sambanova": {
        "name": "SambaNova",
        "api": "openai-completions", "url": "https://api.sambanova.ai/v1",
        "prefix": "", "free": True,
        "info": "Free | cloud.sambanova.ai",
        "env": "SAMBANOVA_API_KEY",
        "models": [
            {"id": "Meta-Llama-3.3-70B-Instruct", "name": "LLaMA 3.3 70B", "cw": 128000, "mt": 8192, "r": False},
            {"id": "DeepSeek-R1", "name": "DeepSeek R1", "cw": 164000, "mt": 16384, "r": True},
        ]
    },
    "deepseek": {
        "name": "DeepSeek (Direct)",
        "api": "openai-completions", "url": "https://api.deepseek.com/v1",
        "prefix": "sk-", "free": False,
        "info": "$0.14/M input | platform.deepseek.com",
        "env": "DEEPSEEK_API_KEY",
        "models": [
            {"id": "deepseek-chat", "name": "DeepSeek V3", "cw": 164000, "mt": 16384, "r": False},
            {"id": "deepseek-reasoner", "name": "DeepSeek R1", "cw": 164000, "mt": 16384, "r": True},
        ]
    },
    "hyperbolic": {
        "name": "Hyperbolic",
        "api": "openai-completions", "url": "https://api.hyperbolic.xyz/v1",
        "prefix": "", "free": True,
        "info": "$10 free | app.hyperbolic.xyz",
        "env": "HYPERBOLIC_API_KEY",
        "models": [
            {"id": "deepseek-ai/DeepSeek-R1", "name": "DeepSeek R1", "cw": 164000, "mt": 16384, "r": True},
            {"id": "Qwen/QwQ-32B", "name": "QwQ 32B", "cw": 128000, "mt": 16384, "r": True},
        ]
    },
}

# ═══════════════════════════════════════════════════════════════════
#  PATHS (actual OpenClaw layout)
# ═══════════════════════════════════════════════════════════════════

AGENT_DIR      = os.path.expanduser("~/.openclaw/agents/main/agent")
AUTH_PROFILES  = os.path.join(AGENT_DIR, "auth-profiles.json")
AUTH_JSON      = os.path.join(AGENT_DIR, "auth.json")
MODELS_JSON    = os.path.join(AGENT_DIR, "models.json")
OPENCLAW_JSON  = os.path.expanduser("~/.openclaw/openclaw.json")
DEVICE_JSON    = os.path.expanduser("~/.openclaw/device.json")
KEYS_FILE      = "keys.txt"

# ═══════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════

def load(p):
    try:
        with open(p) as f: return json.load(f)
    except: return {}

def save(p, d):
    if os.path.exists(p):
        shutil.copy2(p, f"{p}.bak.{int(time.time())}")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, 'w') as f: json.dump(d, f, indent=2)
    print(f"    ✓ {os.path.basename(p)}")

def read_keys():
    if not os.path.exists(KEYS_FILE):
        print(f"\n  ERROR: {KEYS_FILE} not found"); sys.exit(1)
    with open(KEYS_FILE) as f:
        k = [l.strip() for l in f if l.strip() and not l.startswith('#')]
    if not k: print(f"  ERROR: empty"); sys.exit(1)
    return k

def model_obj(m):
    """Match the EXACT schema from your models.json"""
    return {
        "id": m["id"], "name": m["name"],
        "reasoning": m.get("r", False),
        "input": ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": m["cw"], "maxTokens": m["mt"]
    }

# ═══════════════════════════════════════════════════════════════════
#  THE FIVE STEPS
# ═══════════════════════════════════════════════════════════════════

def step_auth_profiles(pk, keys):
    """auth-profiles.json — key pool. MERGES with existing."""
    d = load(AUTH_PROFILES)
    d.setdefault("version", 1)
    d.setdefault("profiles", {})
    d.setdefault("lastGood", {})
    d.setdefault("usageStats", {})
    aliases = []
    for i, key in enumerate(keys, 1):
        a = f"{pk}:key{i}"; aliases.append(a)
        d["profiles"][a] = {"type": "api_key", "provider": pk, "key": key}
        d["usageStats"][a] = {"lastUsed": 0, "errorCount": 0, "lastFailureAt": 0}
    d["lastGood"][pk] = aliases[0]
    print(f"\n  [1/5] auth-profiles.json — {len(keys)} keys")
    save(AUTH_PROFILES, d)
    return aliases

def step_auth_json(pk, key):
    """auth.json — active key. MERGES."""
    d = load(AUTH_JSON)
    d[pk] = {"type": "api_key", "key": key}
    print(f"\n  [2/5] auth.json — active: ...{key[-8:]}")
    save(AUTH_JSON, d)

def step_models_json(pk):
    """models.json — provider + model definitions. MERGES."""
    p = PROVIDERS[pk]; d = load(MODELS_JSON)
    d.setdefault("providers", {})
    entry = {
        "api": p["api"],
        "models": [model_obj(m) for m in p["models"]],
        "apiKey": p["env"]
    }
    if p["url"]: entry["baseUrl"] = p["url"]
    d["providers"][pk] = entry
    print(f"\n  [3/5] models.json — {len(p['models'])} models")
    save(MODELS_JSON, d)

def step_openclaw_json(pk, key):
    """openclaw.json — env, auth.profiles, models.providers, agents.defaults.models.
    Matches YOUR actual file structure exactly. MERGES."""
    p = PROVIDERS[pk]
    c = load(OPENCLAW_JSON)
    if not c: print(f"  ERROR: can't read openclaw.json"); sys.exit(1)

    # env section — set the key value
    c.setdefault("env", {})[p["env"]] = key

    # auth.profiles — register provider (NOT auth.order — that stays as custom-1)
    c.setdefault("auth", {}).setdefault("profiles", {})
    c["auth"]["profiles"][f"{pk}:default"] = {"provider": pk, "mode": "api_key"}

    # models.providers — provider definition referencing ${ENV_VAR}
    c.setdefault("models", {}).setdefault("providers", {})
    prov = {
        "apiKey": "${" + p["env"] + "}",
        "api": p["api"],
        "models": [model_obj(m) for m in p["models"]]
    }
    if p["url"]: prov["baseUrl"] = p["url"]
    c["models"]["providers"][pk] = prov

    # agents.defaults.models — the whitelist
    wl = c.setdefault("agents", {}).setdefault("defaults", {}).setdefault("models", {})
    for m in p["models"]:
        wl[f"{pk}/{m['id']}"] = {}

    print(f"\n  [4/5] openclaw.json — env + auth + models + whitelist")
    save(OPENCLAW_JSON, c)

def step_device():
    """device.json — fresh Ed25519 identity. Privacy rotation."""
    dev = generate_device()
    print(f"\n  [5/5] device.json — new identity")
    save(DEVICE_JSON, dev)
    print(f"    ID: {dev['deviceId'][:16]}...")
    return dev

# ═══════════════════════════════════════════════════════════════════
#  DISPLAY
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
    p = PROVIDERS[pk]; pf = load(AUTH_PROFILES)
    print(f"\n  ╔══════════════════════════════════════════════════╗")
    print(f"  ║  ✅ Done                                         ║")
    print(f"  ╠══════════════════════════════════════════════════╣")
    print(f"  ║  {p['name']:<48} ║")
    print(f"  ║  {len(keys)} keys • {len(p['models'])} models • device rotated       ║")
    print(f"  ╠══════════════════════════════════════════════════╣")
    for a in aliases:
        t = "..." + pf.get("profiles",{}).get(a,{}).get("key","?")[-8:]
        print(f"  ║  {a:<20} {t:<28} ║")
    print(f"  ╠══════════════════════════════════════════════════╣")
    fm = f"{pk}/{p['models'][0]['id']}"
    if len(fm)>44: fm = fm[:41]+"..."
    print(f"  ║  /model {fm:<40} ║")
    print(f"  ╚══════════════════════════════════════════════════╝")

# ═══════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════

def main():
    print("\n  ╔══════════════════════════════════════════════════╗")
    print("  ║  OpenClaw Key Manager v3.0                       ║")
    print("  ║  Keys • Models • Device Rotation                 ║")
    print("  ╚══════════════════════════════════════════════════╝")

    show_providers()
    pkeys = list(PROVIDERS.keys())

    while True:
        c = input(f"\n  Provider (1-{len(pkeys)} or name): ").strip().lower()
        if c.isdigit() and 0 <= int(c)-1 < len(pkeys):
            pk = pkeys[int(c)-1]; break
        elif c in PROVIDERS: pk = c; break
        else:
            m = [k for k in pkeys if c in k]
            if len(m)==1: pk = m[0]; break
        print("  Try again.")

    show_models(pk)
    keys = read_keys()
    print(f"\n  {len(keys)} key(s) from {KEYS_FILE}")

    p = PROVIDERS[pk]
    if p["prefix"]:
        bad = [k for k in keys if not k.startswith(p["prefix"])]
        if bad:
            print(f"  ⚠️  {len(bad)} missing prefix '{p['prefix']}'")
            if input("  Continue? (y/n): ").strip().lower() != 'y': sys.exit(0)

    print(f"\n  {'═'*50}")
    print(f"  {p['name']} — {len(keys)} keys")
    print(f"  {'═'*50}")

    aliases = step_auth_profiles(pk, keys)
    step_auth_json(pk, keys[0])
    step_models_json(pk)
    step_openclaw_json(pk, keys[0])
    step_device()

    show_done(pk, keys, aliases)

    print("\n  Restarting gateway...")
    try:
        r = subprocess.run(["openclaw","gateway","restart"], capture_output=True, text=True, timeout=30)
        print("  ✅ Restarted" if r.returncode==0 else f"  ⚠️  Code {r.returncode}")
    except: print("  ⚠️  Manual: openclaw gateway restart")

    print("\n  🦞\n")

if __name__ == "__main__": main()
