# bestway-spa-v2

[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/epheterson/bestway-spa-v2/badge)](https://scorecard.dev/viewer/?uri=github.com/epheterson/bestway-spa-v2)

Control Bestway SmartSpa / Lay-Z-Spa hot tubs (V02) from the command line.

**No MITM proxy. No dedicated phone. No Charles Proxy.** Just scan a QR code from the app and you're in.

## What is this?

A standalone Python CLI that talks to the Bestway SmartHub cloud API (AWS IoT backend) used by V02 Lay-Z-Spa models — the ones that pair with the **Bestway SmartSpa** app (not the older "Bestway Connect" or "Lay-Z-Spa WiFi" apps).

### The problem

Every existing approach to controlling these spas programmatically required intercepting the mobile app's network traffic with a MITM proxy (Charles Proxy or mitmproxy), installing a custom SSL certificate on your phone, and sometimes downgrading to an older app version. This is fragile, complex, and breaks when Bestway updates the app.

### The solution

The app has a "Share Device" feature that generates a QR code. This tool:

1. Creates an anonymous API visitor (no account needed)
2. Authenticates with the Bestway cloud
3. Uses the QR code to bind your spa to the new visitor
4. Gives you full read/write control via simple CLI commands

## Quick Start

```bash
# Install
git clone https://github.com/epheterson/bestway-spa-v2.git
cd bestway-spa-v2
pip install -r requirements.txt

# Setup (one-time, takes 2 minutes)
python bestway_spa.py setup

# Use
python bestway_spa.py status
python bestway_spa.py heat on
python bestway_spa.py temp 104
python bestway_spa.py filter on
python bestway_spa.py jets on
```

## Setup

Run `python bestway_spa.py setup` and follow the prompts:

1. **Select your region** — EU, US, or CN
2. **Generate a QR code** from the Bestway SmartSpa app:
   - Open the app, tap `...` on your spa
   - Tap **Share The Device**
   - A QR code appears (valid for 15 minutes)
3. **Scan the QR** with your phone's camera (not the Bestway app)
4. **Copy the text** — it starts with `RW_Share_`
5. **Paste it** into the setup prompt

That's it. Your credentials are saved to `.spa_config.json` and persist across sessions.

## Commands

| Command | Description |
|---------|-------------|
| `setup` | One-time setup with QR code |
| `status` | Show current spa state |
| `heat on\|off` | Heater control |
| `temp <value>` | Set target temperature |
| `filter on\|off` | Filter pump |
| `jets on\|off` | Bubbles / wave jets |
| `hydrojet on\|off` | HydroJet (if equipped) |
| `power on\|off` | Master power |
| `raw` | Raw JSON from device shadow |
| `debug` | Full API diagnostic dump |

All commands include read-back verification — after sending a command, the tool waits briefly and reads back the state to confirm it took effect.

## How it works

### Authentication

The Bestway API uses anonymous "visitors" — no email or password. Authentication is:
1. Generate a random `visitor_id`
2. POST to `/api/enduser/visitor` with an MD5-signed request
3. Get back a token (valid ~2 days)

The auth endpoint requires two undocumented fields (`brand: ""` and `marketing_notification: 0`) that cause HTTP 500 if missing. These were discovered through systematic field testing.

### Device binding

The QR code from "Share Device" contains a vercode (format: `RW_Share_<request_id>_<device_id>`). This is POSTed to `/api/enduser/grant_device` to bind the spa to your visitor account.

### Commands

Commands use the v2 encrypted API (`/api/v2/device/command`) with AES-256-CBC encryption, falling back to v1 plaintext if `pycryptodome` is not installed.

### Device discovery

After binding, devices are discovered via: `homes` → `rooms` → `devices` hierarchy.

## Configuration

Config is stored in `.spa_config.json` (gitignored). Override the path with the `BESTWAY_CONFIG` environment variable.

```json
{
  "region": "EU",
  "visitor_id": "abc123...",
  "device_id": "def456...",
  "device_name": "HOTTub",
  "product_id": "FTEW0E",
  "product_series": "ULTRAFIT AIRJET"
}
```

## Security notes

- **SSL verification** is disabled (`verify=False`) because the Bestway API servers use certificates that fail validation in some environments. Traffic is still TLS-encrypted; only certificate chain verification is skipped. This matches the official Android app's behavior.
- **APPID/APPSECRET** are extracted from the publicly downloadable Bestway SmartSpa APK and are identical for all users. They are not personal credentials.
- **Your credentials** (`visitor_id`, `device_id`) are stored locally in `.spa_config.json` (gitignored). Never commit this file.

## Compatible devices

V02 models that use the **Bestway SmartSpa** app, including:
- Lay-Z-Spa Airjet V02
- Lay-Z-Spa HydroJet V02
- Lay-Z-Spa Ultrafit Airjet

If your spa uses the older "Bestway Connect" or "Lay-Z-Spa WiFi" app, it's a V01 (GizWits) device — see [cdpuk/ha-bestway](https://github.com/cdpuk/ha-bestway) instead.

## Home Assistant

A proper Home Assistant custom component is planned. For now, you can use the CLI with shell commands or wrap it in a REST sensor + command_line switch.

For existing HA users: [cdpuk/ha-bestway PR #105](https://github.com/cdpuk/ha-bestway/pull/105) by ashleigh-hopkins adds V02 support with QR setup. This CLI is a standalone alternative that doesn't require HA.

## Credits

This project builds on the reverse engineering work of the Bestway spa community:

- **[M1R4G376/New_bestway_spa](https://github.com/M1R4G376/New_bestway_spa)** — Original V02 API reverse engineering, endpoint discovery, APPID/APPSECRET extraction from decompiled APK
- **[cdpuk/ha-bestway](https://github.com/cdpuk/ha-bestway) ([PR #105](https://github.com/cdpuk/ha-bestway/pull/105) by ashleigh-hopkins)** — QR code binding flow, AES-256-CBC encryption, device discovery via homes/rooms hierarchy, v2 command API
- **Bestway SmartSpa Android APK** (decompiled) — API constants, AES IV, endpoint structure

## License

MIT
