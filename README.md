# BUDYCN -- CodeBuddy.cn Account Creator

```
  ___ ___  ___  ___ ___ _   _ ___  _____   __
 / __/ _ \|   \| __| _ ) | | |   \|   \ \ / /
| (_| (_) | |) | _|| _ \ |_| | |) | |) \ V /
 \___\___/|___/|___|___/\___/|___/|___/ |_|

Account Creator
by dvaa
```

## Features

- **Headless browser** -- Playwright runs without window
- **Rich UI** -- Professional terminal display with panels, tables, progress bar
- **Cross-platform** -- Auto-detect 9router path on Windows, Linux, macOS
- **Batch processing** -- Create many accounts at once with progress bar
- **Proxy checker** -- Test proxy connectivity before use
- **9router injection** -- Auto-inject tokens to 9router database

## Requirements

- Python 3.11+
- Playwright browser (Chromium)

```bash
pip install -r requirements.txt
playwright install chromium
```

## Quick Start

```bash
# Interactive menu
python run.py

# Test all proxies
python run.py proxy

# Create 1 account
python run.py batch -c 1

# Batch 5 accounts
python run.py batch -c 5

# View saved tokens
python run.py tokens

# View configuration
python run.py settings
```

## Configuration

All configuration is in `config.toml`:

```toml
[[proxies]]
label = "LOCAL"
type = "http"
host = "127.0.0.1"
port = 60000

[bot]
country = "hongkong"
service = "codebuddy"
```

## License

Contact **@machine_id_bot** & **@omopagll** on Telegram to get your license key.

This bot is license-protected. You need a valid licaense key to use it.
