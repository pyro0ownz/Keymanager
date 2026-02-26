# 🦞 OpenClaw Key Manager v3.0

Multi-provider API key rotation and device identity management for [OpenClaw](https://github.com/nichochar/openclaw) (2026.2.24+).

Automates the entire setup pipeline: reads your API keys from a file, registers providers and models across all five OpenClaw config files, whitelists models, and rotates your device fingerprint for privacy — in one command.

## What It Does

```
keys.txt → auth-profiles.json → auth.json → models.json → openclaw.json → device.json
```

1. **Reads `keys.txt`** — one API key per line, supports comments with `#`
2. **Builds key pool** — creates numbered auth profiles with usage tracking in `auth-profiles.json`
3. **Sets active key** — writes the first key as the active provider key in `auth.json`
4. **Registers models** — adds the provider and all its models (with full schema) to `models.json`
5. **Updates main config** — injects env vars, auth profiles, model providers, and whitelists into `openclaw.json`
6. **Rotates device identity** — generates a fresh Ed25519 keypair and device ID in `device.json` so providers can't correlate sessions across key rotations
7. **Restarts the gateway** — applies everything automatically

All writes are **merge operations** — existing providers and keys from previous runs are preserved.

## Quick Start

```bash
# Put your API keys in keys.txt (one per line)
echo "AIzaSyBSEB38...." > keys.txt
echo "AIzaSyBR0Kz7...." >> keys.txt
echo "AIzaSyBaHQ6w...." >> keys.txt

# Run it
python3 openclaw_key_manage.py

# Pick your provider from the menu, done
```

## Multi-Provider Stacking

Run it once per provider. Keys merge, nothing gets overwritten.

```bash
# Google — 18 keys
cat google_keys.txt > keys.txt
python3 openclaw_key_manage.py   # select 1

# NVIDIA NIM — 1 key
cat nvidia_keys.txt > keys.txt
python3 openclaw_key_manage.py   # select 3

# Groq — 5 keys
cat groq_keys.txt > keys.txt
python3 openclaw_key_manage.py   # select 2
```

After setup, switch models in the OpenClaw chat:

```
/model google/gemini-2.5-flash
/model nvidia-nim/moonshotai/kimi-k2.5
/model groq/llama-3.3-70b-versatile
```

## Supported Providers

| # | Provider | Free Tier | Models | Get Keys |
|---|----------|-----------|--------|----------|
| 1 | Google Gemini | 🟢 15 RPM, 1M TPD | 6 | [ai.google.dev](https://ai.google.dev) |
| 2 | Groq | 🟢 30 RPM | 6 | [console.groq.com](https://console.groq.com) |
| 3 | NVIDIA NIM | 🟢 1000 req/day | 8 | [build.nvidia.com](https://build.nvidia.com) |
| 4 | OpenRouter | 🟢 Free models | 4 | [openrouter.ai](https://openrouter.ai) |
| 5 | Mistral AI | 🟢 Free tier | 3 | [console.mistral.ai](https://console.mistral.ai) |
| 6 | Together AI | 🟢 $5 credit | 3 | [api.together.ai](https://api.together.ai) |
| 7 | Cerebras | 🟢 30 RPM | 2 | [cloud.cerebras.ai](https://cloud.cerebras.ai) |
| 8 | SambaNova | 🟢 Free tier | 2 | [cloud.sambanova.ai](https://cloud.sambanova.ai) |
| 9 | DeepSeek | 💰 $0.14/M input | 2 | [platform.deepseek.com](https://platform.deepseek.com) |
| 10 | Hyperbolic | 🟢 $10 credit | 2 | [app.hyperbolic.xyz](https://app.hyperbolic.xyz) |

## Files Modified

| File | Location | Purpose |
|------|----------|---------|
| `auth-profiles.json` | `~/.openclaw/agents/main/agent/` | Key pool with `lastGood` and `usageStats` per key |
| `auth.json` | `~/.openclaw/agents/main/agent/` | Active key per provider |
| `models.json` | `~/.openclaw/agents/main/agent/` | Provider definitions with full model schema (`reasoning`, `input`, `cost`, `contextWindow`, `maxTokens`) |
| `openclaw.json` | `~/.openclaw/` | Env vars, auth profiles, model providers with `${ENV_VAR}` references, and the model whitelist under `agents.defaults.models` |
| `device.json` | `~/.openclaw/` | Ed25519 device identity (rotated each run) |

Every file is backed up with a timestamp before writing (e.g., `auth-profiles.json.bak.1740500000`).

## Device Identity Rotation

Each run generates a new `device.json` with:

- Fresh SHA-256 device ID from 64 bytes of cryptographic randomness
- New Ed25519 keypair (PEM-encoded PKCS8 format)
- Updated `createdAtMs` timestamp

This prevents API providers from correlating your sessions across key rotations. The script tries three methods for Ed25519 generation in order:

1. Python `cryptography` library
2. `openssl` CLI
3. `PyNaCl`

At least one of these is available on any standard Linux install.

## Key Prefix Validation

The script validates key prefixes before writing:

| Provider | Expected Prefix |
|----------|----------------|
| Google | `AIzaSy` |
| Groq | `gsk_` |
| NVIDIA NIM | `nvapi-` |
| OpenRouter | `sk-or-` |
| Cerebras | `csk-` |

Mismatched prefixes trigger a warning with the option to continue or abort.

## Requirements

- Python 3.8+
- OpenClaw 2026.2.24+
- One of: `cryptography`, `openssl`, or `PyNaCl` (for Ed25519 device rotation)

```bash
# If you need cryptography:
pip install cryptography --break-system-packages
```

## keys.txt Format

```
# Google Gemini keys
AIzaSyBS....
AIzaSyBR.....

# Blank lines and comments are ignored
AIzaSyB....
```

## Contributing

Add new providers by extending the `PROVIDERS` dictionary. Each entry needs:

```python
"provider-name": {
    "name": "Display Name",
    "api": "openai-completions",  # or "google" for native
    "url": "https://api.example.com/v1",  # None for Google
    "prefix": "sk-",  # key prefix for validation, "" to skip
    "free": True,
    "info": "Rate limits | signup URL",
    "env": "PROVIDER_API_KEY",  # env var name in openclaw.json
    "models": [
        {"id": "model-id", "name": "Display Name", "cw": 128000, "mt": 8192, "r": False},
    ]
}
```

## License

MIT
