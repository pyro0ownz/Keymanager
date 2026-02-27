# 🦞 OpenClaw Key Manager v3.2

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

## Commands

```bash
python3 openclaw_key_manage.py                # Interactive provider setup
python3 openclaw_key_manage.py --status        # Show all keys and providers
python3 openclaw_key_manage.py --fix           # Repair broken configs (v3.0 → v3.2)
python3 openclaw_key_manage.py --remove google # Remove a provider cleanly
python3 openclaw_key_manage.py --help          # Usage info
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

Every file is backed up with a timestamp before writing (e.g., `auth-profiles.json.bak.20260227_143052`).

## Native vs OpenAI-Compatible Providers

Google is handled as a **native provider** in OpenClaw. The key manager configures it through `env`, `auth.profiles`, and the model whitelist only — it does **not** inject Google into `models.providers`. OpenClaw manages the Google API connection internally.

All other providers (Groq, NVIDIA NIM, OpenRouter, etc.) are configured as **OpenAI-compatible** through `models.providers` with `"api": "openai-completions"` and their respective `baseUrl`.

> **If you're upgrading from v3.0:** Run `python3 openclaw_key_manage.py --fix` to remove the invalid Google provider entry that was breaking `openclaw.json` validation.

## Config Repair (`--fix`)

If your config was broken by v3.0 (the `"api": "google"` bug), run:

```bash
python3 openclaw_key_manage.py --fix
openclaw gateway restart
```

This removes the invalid Google entry from `models.providers` and cleans up any empty `baseUrl` fields. Your other providers (nvidia-nim, groq, etc.) are left untouched.

## Key Pool Status (`--status`)

Check the health of all configured keys:

```bash
python3 openclaw_key_manage.py --status
```

Shows per-key error counts, cooldown status, active key markers, and model counts per provider.

## Provider Removal (`--remove`)

Cleanly strip a provider from all five config files:

```bash
python3 openclaw_key_manage.py --remove groq
```

Removes the provider's keys from `auth-profiles.json`, `auth.json`, `models.json`, `openclaw.json` (env, auth, models, whitelist), and confirms before writing.

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

## Changelog

### v3.2
- **FIXED:** Google handled as native provider — no longer injected into `models.providers` (was causing `"api": "google"` validation error that broke OpenClaw configs)
- **FIXED:** `baseUrl: undefined` no longer written for providers without a base URL
- **FIXED:** Unicode/emoji encoding crash on terminals with restricted locale — pure ASCII output
- **ADDED:** `--fix` command to repair broken v3.0 configs automatically
- **ADDED:** `--status` command to show all keys, error counts, cooldowns, and active key per provider
- **ADDED:** `--remove` command to cleanly strip a provider from all config files
- **ADDED:** `--help` command
- **ADDED:** Pre-flight checks — verifies OpenClaw installation before writing any files
- **ADDED:** Duplicate key detection — re-running doesn't create duplicate pool entries
- **IMPROVED:** Backup filenames now use ISO timestamps instead of unix epoch
- **IMPROVED:** Key file parser handles inline comments and whitespace
- **IMPROVED:** Error handling on file operations, keyboard interrupts, missing OpenClaw

### v3.0
- Initial multi-provider release with 10 providers and device identity rotation
- **Known issue:** Google provider injection into `models.providers` with `"api": "google"` breaks OpenClaw config validation. Upgrade to v3.2 and run `--fix`.

## Contributing

Add new providers by extending the `PROVIDERS` dictionary. Each entry needs:

```python
"provider-name": {
    "name": "Display Name",
    "api": "openai-completions",
    "url": "https://api.example.com/v1",  # None for native providers
    "prefix": "sk-",  # key prefix for validation, "" to skip
    "free": True,
    "native": False,  # True only for providers OpenClaw handles internally (Google)
    "info": "Rate limits | signup URL",
    "env": "PROVIDER_API_KEY",  # env var name in openclaw.json
    "models": [
        {"id": "model-id", "name": "Display Name", "cw": 128000, "mt": 8192, "r": False},
    ]
}
```

# Gemini API Key Tester

Bulk-test your Google Gemini API keys against all available models. Reads keys from a file, tests each one, rates their health, and saves a report.

## Quick Start

```bash
# Put your keys in a file
echo "AIzaSyBS...." > keys.txt
echo "AIzaSyBR...." >> keys.txt
echo "AIzaSyBa...." >> keys.txt

# Run it
chmod +x gemini_key_tester.sh
./gemini_key_tester.sh keys.txt
```

## Usage

```
./gemini_key_tester.sh <keys_file> [mode]
```

| Mode | Description |
|------|-------------|
| *(none)* | Full test — every key against all 23 models |
| `--summary` | Pass/fail counts only, no per-model output |
| `--fast` | Quick health check — 3 core models per key |

## How It Works

1. Reads `keys.txt` (one key per line, `#` comments supported)
2. Validates key prefix (`AIzaSy`)
3. Sends a minimal `"ping"` request to each model's `generateContent` endpoint
4. Classifies the response:

| Result | Meaning |
|--------|---------|
| **OK** | Model responded successfully |
| **429 QUOTA** | Key hit rate limit — still valid, just exhausted |
| **404 NOT FOUND** | Model not available for this key/project |
| **DENIED** | API not enabled in Google Cloud Console |
| **INVALID KEY** | Key is dead — skips remaining models |
| **ERR** | Other error (timeout, network, etc.) |

5. Rates each key's overall health:

| Rating | Criteria |
|--------|----------|
| **GOOD** | 50%+ models responded |
| **PARTIAL** | Some models responded |
| **EXHAUSTED** | All quota errors — valid but rate limited |
| **DEAD** | No successful responses |

## Models Tested

### Full Mode (23 models)

```
gemini-1.5-flash              gemini-2.5-flash-preview-04-17
gemini-1.5-flash-8b           gemini-2.5-flash-preview-05-20
gemini-1.5-pro                gemini-2.5-flash-preview-09-2025
gemini-2.0-flash              gemini-2.5-pro
gemini-2.0-flash-lite         gemini-2.5-pro-preview-05-06
gemini-2.5-flash              gemini-2.5-pro-preview-06-05
gemini-2.5-flash-lite         gemini-3-flash-preview
gemini-2.5-flash-lite-06-17   gemini-3-pro-preview
gemini-2.5-flash-lite-09      gemini-3.1-pro-preview
gemini-flash-latest           gemini-3.1-pro-preview-customtools
gemini-flash-lite-latest      gemini-live-2.5-flash
                              gemini-live-2.5-flash-preview-native
```

### Fast Mode (3 models)

```
gemini-2.0-flash
gemini-2.5-flash
gemini-3-flash-preview
```

## Output

Each run saves a timestamped report:

```
key_test_report_20260227_143052.txt
```

Example output:

```
==========================================
 Gemini API Key Tester
 Keys:   10 (from keys.txt)
 Models: 23
 Mode:   full
==========================================

------------------------------------------
 Key 1/10: ...WaW8
------------------------------------------
  [Test] gemini-2.0-flash: [OK]
  [Test] gemini-2.5-flash: [OK]
  [Test] gemini-3-flash-preview: [OK]
  ...

  Results for ...WaW8:
    OK: 18  |  Quota: 0  |  404: 3  |  Denied: 0  |  Error: 2
    Health: GOOD

==========================================
 SUMMARY
==========================================
  Key 1  (...WaW8): GOOD - OK:18 Quota:0 404:3 Denied:0 Err:2
  Key 2  (...GH8A): EXHAUSTED - OK:0 Quota:23 404:0 Denied:0 Err:0
  Key 3  (...xK9m): DEAD - OK:0 Quota:0 404:0 Denied:23 Err:0

  Usable keys: 1 / 3
==========================================
```

## keys.txt Format

```
# Google Gemini API keys
# Generated from AI Studio: ai.google.dev

AIzaSyBS....
AIzaSyBR....

# This key is from project 2
AIzaSyBa....

# Blank lines and comments are ignored
```

## Requirements

- `bash`
- `curl`
- Network access to `generativelanguage.googleapis.com`

## Pairing with Key Manager

Run the tester first to verify your keys, then feed the good ones to the key manager:

```bash
# Test all keys
./gemini_key_tester.sh all_keys.txt --fast

# Remove dead keys, keep good ones in keys.txt
# Then run the key manager
python3 openclaw_key_manage.py
```

# OpenClaw Key Rotator v2.0 (The Watcher) rotateWatch.py

An active, auto-rotating API key management daemon for OpenClaw. Unlike passive libraries, this version includes a real-time **Log Watcher** that monitors OpenClaw's output and swaps keys the millisecond a rate limit (`429`) or suspension (`403`) is detected.

## 🛠 Why v2.0?
* **The Problem with v1.0:** It was a "Passive Library." It contained the methods to rotate, but OpenClaw never called them. When a key hit a limit, the gateway would hang on "Compacting..." while infinitely retrying a dead key.
* **The v2.0 Solution:** An "Active Watcher." It tails your OpenClaw logs. When it detects `RESOURCE_EXHAUSTED` or `PERMISSION_DENIED`, it immediately updates `auth.json` and `openclaw.json` with a fresh key from your pool.

## 🚀 Installation & Setup

1.  **Deploy the Script:**
    Place `rotateWatcher.py` in your OpenClaw home directory (usually `~/.openclaw/workspace`).

2.  **Initialize your Key Pool:**
    Ensure your `auth-profiles.json` is populated with your Google/NVIDIA/Groq keys.

3.  **Launch the Watcher (The "Auto-Immune" Mode):**
    Run this alongside your OpenClaw gateway:
    ```bash
    openclaw gateway logs -f | python3 key_rotator.py watch &
    ```
    *This pipes live logs into the rotator. It will now handle all 429 errors in the background.*

## 📈 Key Features
* **Real-Time Pipe Monitoring:** Uses `stdin` piping to catch errors as they happen.
* **Smart Cooldowns:** Implements a 65-second "sin bin" for rate-limited keys (matching Google's 60s reset).
* **Dead Key Detection:** Automatically flags suspended or invalid keys (Error 403) and removes them from the rotation pool.
* **Pure ASCII Output:** Zero-dependency, terminal-friendly output for high-performance/low-resource environments.
* **Dual-Config Updates:** Simultaneously updates the active workspace `auth.json` and the global `openclaw.json` environment variables.

## 🕹 CLI Commands

| Command | Description |
| :--- | :--- |
| `python3 key_rotator.py status` | View health, error counts, and cooldown status of all keys. |
| `python3 key_rotator.py test` | Performs a live "ping" test on the active key to verify connectivity. |
| `python3 key_rotator.py rotate` | Force an immediate swap to the next healthiest key in the pool. |
| `python3 key_rotator.py reset` | Clear all error counts and cooldowns for a fresh start. |

## ⚠️ Operational Note: Operation I am alive!
During high-stakes monitoring keep the `watch` command running in a background screen or tmux session. If the gateway hangs on "Compacting," the watcher will detect the log-jam and force a key swap to break the database deadlock.

---
*Note: This tool is designed to work with OpenClaw 2026 and Google Gemini API rate-limiting patterns.*

## License

MIT
