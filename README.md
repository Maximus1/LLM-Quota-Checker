# LLM Quota Checker

A desktop GUI application that monitors the availability of LLM API accounts across multiple providers. It checks quota limits automatically on a configurable interval, shows live countdowns for rate-limited models, and helps you discover which models are available and free on your API keys.

---

## Features

**Account Monitoring**
- Monitors multiple API accounts and models simultaneously
- Configurable check interval (default: 60 minutes)
- Live countdown timer when a model is rate-limited — skips re-checking until the cooldown expires
- "Check Now" button to force an immediate check regardless of cooldowns
- Persistent storage: all accounts and their last-known status survive restarts

**Status Classification**
| Status | Meaning |
|---|---|
| ✔ Available | Quota is currently usable |
| ✔ Available ★ | Available and confirmed free ($0 per token) |
| ✖ Limited | HTTP 429 – daily/hourly quota reached, cooldown active |
| 💳 No Balance | HTTP 402 – model requires credits or a paid account |
| ⚠ Error | Other error (connection, 403, etc.) |

**Model Scanner**
- Loads the full model list from the OpenRouter API (no auth required, ~300+ models)
- For NVIDIA NIM: fetches the model list directly from `/v1/models` using your API key
- Tests each model in parallel (4 concurrent workers) against your API key
- Shows live results as they come in, with price data from OpenRouter
- Distinguishes between confirmed-free models (`:free` suffix), assumed-free ($0 price), and models that return 402 despite a $0 price listing
- Filter results by status; select individual models or bulk-add all available ones as accounts
- Custom model IDs can be added to the test list manually

**System Tray**
- Minimizes to the system tray (both via the X button and the minimize button)
- Tray icon color reflects overall status: 🟢 all available · 🟡 some limited · 🔴 all limited
- Tooltip shows per-account status with remaining cooldown time
- Right-click menu: Open, Check Now, Quit

**Account Management**
- Each account is identified by an internal UUID — the display name (provider) can appear multiple times without conflicts
- Add, edit, delete accounts via dialog; API key field is masked with a visibility toggle
- Test connection directly in the add/edit dialog before saving
- Sortable columns in both the main table and the scan results table

---

## Supported Providers

| Provider | Completions URL | Model Discovery |
|---|---|---|
| Cline | `https://api.cline.bot/api/v1/chat/completions` | via OpenRouter API |
| OpenRouter | `https://openrouter.ai/api/v1/chat/completions` | via OpenRouter API |
| NVIDIA NIM | `https://integrate.api.nvidia.com/v1/chat/completions` | via `/v1/models` (auth required) |
| Any OpenAI-compatible API | custom URL | manual model IDs |

---

## Installation

**Requirements:** Python 3.10+

```bash
pip install requests pystray pillow
```

Or use the included Windows batch file which installs dependencies automatically.

**Run:**
```bash
python llm_quota_checker_gui.py
```

**Windows:** double-click `LLM_Quota_Checker_Starten.bat`

To start automatically on Windows login, place a shortcut to the `.bat` file in:
```
shell:startup
```

---

## Usage

### Adding accounts manually

1. Click **＋ Add** in the toolbar
2. Fill in:
   - **Name** — the provider label (e.g. `Cline`, `OpenRouter`). Not unique — multiple accounts can share the same name.
   - **API Key** — your secret key for this provider
   - **Model** — the model ID in `provider/model-name` format (e.g. `xiaomi/mimo-v2.5`)
   - **URL** — the completions endpoint
3. Click **Test Connection** to verify before saving

### Discovering available models (Scan)

1. Select an existing account in the table (optional — pre-fills key and URL)
2. Click **🔍 Scan Models** in the toolbar
3. Enter API Key and URL if not pre-filled
4. Click **▶ Start Scan**
   - The full model list is fetched from OpenRouter (or NVIDIA NIM)
   - Each model is tested against your API key in parallel
   - Results appear live in the table with status and price info
5. Select individual models (Ctrl+click / Shift+click) or leave nothing selected to add all available ones
6. Click **＋ Add Selection as Account**

### Cooldown logic

When a model returns HTTP 429, the response is parsed for a wait time (e.g. `Try again in 3h 22m`). The model is then skipped automatically until that time has passed. The main table shows a live countdown: `Cooldown – noch 2h 47m (free at 15:22:00)`.

**Check Now** (⟳) always bypasses cooldowns and forces a full re-check of all accounts.

HTTP 402 responses (no balance / credits exhausted) set a 24-hour skip — retrying every hour serves no purpose since balance doesn't replenish automatically.

---

## Configuration

Settings are stored in `~/.llm_quota_checker.json` and are loaded automatically on startup.

```json
{
  "interval_minutes": 60,
  "accounts": [
    {
      "id": "a1b2c3d4",
      "name": "Cline",
      "api_key": "sk_...",
      "model": "xiaomi/mimo-v2.5",
      "url": "https://api.cline.bot/api/v1/chat/completions",
      "status": "ok",
      "detail": "",
      "last_check": "14:22:01",
      "retry_until": null
    }
  ]
}
```

The interval can also be changed at runtime in the toolbar — press Enter to apply.

---

## Free Model Detection

Free model detection is fully dynamic — no hardcoded model lists.

**OpenRouter / Cline:**
A model is considered free if:
1. Its ID ends with `:free` (explicit OpenRouter free tier), **or**
2. Its price from the OpenRouter API is $0 per token

If the actual API call returns HTTP 402, the model is reclassified as **💳 Kostenpflichtig** regardless of the listed price. This catches models that are listed as $0 but still require a paid account on the target provider.

**NVIDIA NIM:**
NVIDIA uses a credits system. New accounts receive 1,000 free inference credits. After that, all models require credits. The app shows `Credits required` for NVIDIA models and handles their different error format (`{"status": 429, "title": "Too Many Requests"}`).

---

## Dependencies

| Package | Purpose |
|---|---|
| `requests` | HTTP calls to LLM APIs and OpenRouter |
| `pystray` | System tray icon |
| `Pillow` | Tray icon image generation |
| `tkinter` | GUI (included with Python) |

---

## Notes

- The app sends a minimal test request (`max_tokens: 1`, message: `"Hi"`) to check quota — this uses negligible tokens
- Parallel scan workers are capped at 4 to avoid triggering rate limits during discovery
- The tray tooltip is limited to 127 characters (Windows system limit)
