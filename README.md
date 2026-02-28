# 🦞 OpenClaw Key Manager v4.0

Multi-provider API key rotation and device identity management for [OpenClaw](https://github.com/nichochar/openclaw) (2026.2.24+).

Automates the entire setup pipeline: reads your API keys from a file, registers providers and models across all five OpenClaw config files, whitelists models, and initializes bucket metadata for project-level rotation — in one command.

## What It Does

```
keys.txt → auth-profiles.json → auth.json → models.json → openclaw.json
```

1. **Reads `keys.txt`** — one API key per line, supports comments with `#` and `bucket=` tags
2. **Builds key pool** — creates numbered auth profiles with usage tracking and bucket metadata in `auth-profiles.json`
3. **Sets active key** — writes the first key as the active provider key in `auth.json`
4. **Registers models** — adds the provider and all its models (with full schema) to `models.json`
5. **Updates main config** — injects env vars, auth profiles, model providers, and whitelists into `openclaw.json`
6. **Initializes bucket stats** — creates `bucketStats` entries for project-level cooldown tracking

All writes are **atomic** (temp file + fsync + rename) with **file locking** to prevent corruption when the rotation daemon runs simultaneously.

## Quick Start

```bash
# Put your API keys in keys.txt (with optional bucket tags)
echo "AIzaSyBSEB38.... # bucket=projA" > keys.txt
echo "AIzaSyBR0Kz7.... # bucket=projA" >> keys.txt
echo "AIzaSyBaHQ6w.... # bucket=projB" >> keys.txt

# Run it
python3 openclaw_key_manage.py

# Pick your provider from the menu, done
```

## Commands

```bash
python3 openclaw_key_manage.py                    # Interactive provider setup
python3 openclaw_key_manage.py --status            # Show all keys, buckets, cooldowns
python3 openclaw_key_manage.py --fix               # Repair broken configs (v3.x → v4.0)
python3 openclaw_key_manage.py --remove google     # Remove a provider cleanly
python3 openclaw_key_manage.py --rotate-device     # Setup + rotate device identity
python3 openclaw_key_manage.py --no-restart        # Setup without restarting gateway
python3 openclaw_key_manage.py --help              # Usage info
```

Flags can be combined: `--rotate-device --no-restart`

## Bucket Support (Google Projects)

Gemini API quota is enforced at the **project level**, not per key. Multiple keys in the same Google project share the same quota. Bucket tags let the rotation daemon cool down an entire project when it hits 429, and switch to a key from a different project.

### keys.txt Format

```
# Project A keys (same Google Cloud project)
AIzaSyBSEB38.... # bucket=projA
AIzaSyBR0Kz7.... # bucket=projA

# Project B keys (different project = different quota)
AIzaSyBaHQ6w.... # bucket=projB

# Project C
AIzaSyDk9m12.... # bucket=projC
AIzaSyX7Pq4f.... # bucket=projC

# No bucket tag = bucket "default"
AIzaSy0000000...
```

### How Buckets Work

1. Key Manager stores `bucket` on each profile in `auth-profiles.json`
2. Key Manager creates `bucketStats` entries per `provider/bucket`
3. Rotation daemon reads bucket metadata and does **project-level cooldown**
4. On 429: daemon cools down the bucket, picks a key from a **different** bucket
5. Exponential backoff: `min(600s, 15s * 2^streak) + jitter`

### auth-profiles.json (what the manager creates)

```json
{
  "profiles": {
    "google:key1": { "provider": "google", "key": "AIzaSy...", "bucket": "projA" },
    "google:key2": { "provider": "google", "key": "AIzaSy...", "bucket": "projB" }
  },
  "bucketStats": {
    "google/projA": { "cooldownUntilMs": 0, "consecutive429": 0, "last429AtMs": 0 },
    "google/projB": { "cooldownUntilMs": 0, "consecutive429": 0, "last429AtMs": 0 }
  }
}
```

## Multi-Provider Stacking

Run it once per provider. Keys merge, nothing gets overwritten.

```bash
# Google — 17 keys across 3 projects
python3 openclaw_key_manage.py   # select 1

# NVIDIA NIM — 1 key
python3 openclaw_key_manage.py   # select 3

# Groq — 5 keys
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

## Native vs OpenAI-Compatible Providers

Google is handled as a **native provider** in OpenClaw. The key manager configures it through `env`, `auth.profiles`, and the model whitelist only — it does **not** inject Google into `models.providers`. OpenClaw manages the Google API connection internally.

All other providers are configured as **OpenAI-compatible** through `models.providers` with `"api": "openai-completions"` and their respective `baseUrl`.

> **Upgrading from v3.x?** Run `python3 openclaw_key_manage.py --fix` to remove the invalid Google provider entry.

## Files Modified

| File | Location | Purpose |
|------|----------|---------|
| `auth-profiles.json` | `~/.openclaw/agents/main/agent/` | Key pool with bucket tags, `usageStats`, and `bucketStats` |
| `auth.json` | `~/.openclaw/agents/main/agent/` | Active key per provider |
| `models.json` | `~/.openclaw/agents/main/agent/` | Provider definitions with full model schema |
| `openclaw.json` | `~/.openclaw/` | Env vars, auth profiles, model providers, whitelist |
| `device.json` | `~/.openclaw/` | Ed25519 device identity (**opt-in** with `--rotate-device`) |

All writes are atomic (temp + fsync + rename) with `fcntl` file locking.

## Device Identity Rotation

Device rotation is **opt-in** via `--rotate-device`. It does NOT increase provider quota. Google rate-limits by project/key/IP, not by OpenClaw device identity.

## Key Prefix Validation

| Provider | Expected Prefix |
|----------|----------------|
| Google | `AIzaSy` |
| Groq | `gsk_` |
| NVIDIA NIM | `nvapi-` |
| OpenRouter | `sk-or-` |
| Cerebras | `csk-` |

## Requirements

- Python 3.8+
- OpenClaw 2026.2.24+
- One of: `cryptography`, `openssl`, or `PyNaCl` (only with `--rotate-device`)

## Changelog

### v4.0
- **ADDED:** Bucket/project support in keys.txt (`# bucket=projA`)
- **ADDED:** `bucketStats` in auth-profiles.json for project-level cooldown
- **ADDED:** Atomic writes (temp file + fsync + rename) to prevent corruption
- **ADDED:** File locking (`fcntl`) to prevent races with rotation daemon
- **ADDED:** `--no-restart` flag (for foreground `openclaw gateway run` users)
- **CHANGED:** Device rotation is **opt-in** via `--rotate-device`
- **CHANGED:** `--status` counts models from whitelist (authoritative), not models.json
- **CHANGED:** Steps now 4 by default (device rotation is optional 5th)
- **FIXED:** Duplicate-keys-only crash in `step_auth_profiles()` (empty alias guard)
- **FIXED:** Removed unused `random` import
- **FIXED:** `read_keys()` returns structured entries with bucket metadata

### v3.2
- Google as native provider, `--fix` / `--status` / `--remove`, pure ASCII

### v3.0
- Initial multi-provider release (**Known issue:** Google `"api": "google"` broke validation)

## Contributing

```python
"provider-name": {
    "name": "Display Name",
    "api": "openai-completions",
    "url": "https://api.example.com/v1",
    "prefix": "sk-",
    "free": True,
    "native": False,
    "info": "Rate limits | signup URL",
    "env": "PROVIDER_API_KEY",
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

🦐 OpenClaw Key Rotation Daemon v3.0
Bucket-aware API key rotation with exponential backoff for OpenClaw (2026.2.24+).
Watches for rate limit errors and automatically switches to a key from a different Google project (bucket). Cools down the entire project on 429, not just one key. Only writes auth.json — confirmed to take effect without gateway restart.
Quick Start
bash# Start the daemon in pipe mode (fastest)
openclaw logs --follow | python3 key_rotator.py watch

# Or let it auto-detect log locations
python3 key_rotator.py watch
Commands
bashpython3 key_rotator.py              # Show status (default)
python3 key_rotator.py status       # Show all keys + bucket cooldowns
python3 key_rotator.py rotate       # Force rotate to next bucket/key
python3 key_rotator.py reset        # Reset all cooldowns and error counts
python3 key_rotator.py test         # Test active key + auto-rotate if bad
python3 key_rotator.py health       # Quick health ping (no rotation)
python3 key_rotator.py watch        # Start auto-rotation daemon
How It Works
Error Classification
Error TypeSignalActionRate limit429, RESOURCE_EXHAUSTED, API rate limit reachedCool down bucket, rotate to different bucketDead keyAPI_KEY_INVALID, PERMISSION_DENIEDMark key dead (errorCount=100), rotateTransient500, 502, 503, UNAVAILABLELog and monitor, no rotation
Bucket-Level Cooldown
Gemini quota is enforced per Google project, not per API key. When a 429 is detected:

The entire bucket (Google project) goes into cooldown
The daemon picks a key from a different bucket
Backoff is exponential: min(600s, 15s * 2^streak) + jitter
On success, the streak resets to 0

On 429:
  bucket "projA" → cooldown 15s   (streak 1)
  next 429:      → cooldown 30s   (streak 2)
  next 429:      → cooldown 60s   (streak 3)
  next 429:      → cooldown 120s  (streak 4)
  ...capped at:  → cooldown 600s  (streak N)

On success:
  streak reset to 0, cooldown cleared
Key Selection Algorithm
1. Skip keys whose bucket is in cooldown (cooldownUntilMs > now)
2. Skip dead keys (errorCount >= 100)
3. Pick least-recently-used among remaining keys
4. If ALL buckets cooling: pick the one expiring soonest, report wait time
What Gets Written
Only auth.json is updated on rotation (no gateway restart needed).
The daemon does NOT touch:

openclaw.json (no env rewrites)
device.json (no identity rotation)
models.json (no model changes)

The daemon does NOT create .bak backup files — its save_json is atomic (temp + rename) but backup-free to avoid disk explosion on frequent 429 events.
All writes use fcntl file locking, matching the Key Manager's I/O format.
Watch Modes
Pipe Mode (fastest)
bashopenclaw logs --follow | python3 key_rotator.py watch
Reads log output line-by-line. Rotates the moment it sees a rate limit pattern.
Auto-Detect Mode
bashpython3 key_rotator.py watch
Checks in order:

/tmp/openclaw/openclaw-YYYY-MM-DD.log (OpenClaw default)
~/.openclaw/logs/gateway.log (legacy)
openclaw logs --follow subprocess
Polling mode (fallback)

Polling Mode (fallback)
Tests active key every 30s with a minimal generateContent request (1 token). On 429, rotates. On success, clears cooldown.
Status Output
$ python3 key_rotator.py status

  -- google (5 keys) --
    BUCKET [projA]: COOLING 42s (streak: 2)
    BUCKET [projB]: [OK]
    BUCKET [projC]: ready (last streak: 1)
  Name                     Err   Bucket       Status
  ------------------------------------------------------------
  google:key1              2     projA        [--]
  google:key2              0     projA        [OK]
  google:key3              0     projB        [OK] < ACTIVE
  google:key4              100   projC        [DEAD]
  google:key5              0     projC        [OK]

  Total: 5 keys | Healthy: 3 | Buckets cooling: 1 | Dead: 1
Pairing with Key Manager v4.0
bash# 1. Provision keys with bucket tags
echo "AIzaSy... # bucket=projA" > keys.txt
echo "AIzaSy... # bucket=projB" >> keys.txt
python3 openclaw_key_manage.py --no-restart

# 2. Start gateway foreground
openclaw gateway run

# 3. In second terminal, start daemon
openclaw logs --follow | python3 key_rotator.py watch

#Data Flow
Key Manager (provisioning, runs once per provider):
keys.txt → auth-profiles.json (bucket + stats) → auth.json → openclaw.json

#Rotator (runtime, runs continuously):
  OpenClaw logs → detect 429 → read bucket from auth-profiles.json
               → set bucket cooldown (exponential backoff)
               → pick key from different bucket
               → write auth.json only (atomic, no restart)
#Concurrency Note
With maxConcurrent: 4 and subagents.maxConcurrent: 8, up to 12 parallel requests can hit the same key. At 15 RPM free tier, one burst exhausts a key instantly.
While testing rotation, set maxConcurrent: 1 in openclaw.json.
#Configuration
SettingDefaultDescription BACKOFF_BASE_SECONDS15 First cooldown duration BACKOFF_MAX_SECONDS600 Maximum cooldown (10 min cap) BACKOFF_JITTER_MAX2.0 Random jitter(seconds) KEY_COOLDOWN_SECONDS65 Per-key cooldown for non-bucket providers MIN_ROTATION_INTERVAL 5 Minimum seconds between rotations POLL_INTERVAL 30 Health check interval in polling mode

#Requirements

Python 3.8+
OpenClaw 2026.2.24+
#Key Manager v4.0 (auth-profiles.json with bucket metadata)

#Changelog v3.0

Bucket-aware rotation (project-level cooldown)
Exponential backoff with jitter
Three-tier error classification (rate_limit / dead / transient)
Correct log paths and CLI (openclaw logs --follow)
Only writes auth.json (no env/restart/device)
Atomic writes, no backup file creation
health command for lightweight ping

#v2.0

Key-level cooldown, pattern matching, three watch modes

#v1.0

Passive library (nothing called it at runtime)
## License

apache2.0
