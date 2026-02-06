#!/usr/bin/env python3
"""
bestway-spa-v2 — Control Bestway SmartSpa / Lay-Z-Spa hot tubs via the cloud API.

No MITM proxy needed. Uses the app's "Share Device" QR code to bind your spa.

Usage:
    bestway_spa.py setup              # One-time setup (scan QR code)
    bestway_spa.py status             # Current spa state
    bestway_spa.py heat on|off        # Heater control
    bestway_spa.py temp <value>       # Set target temperature
    bestway_spa.py filter on|off      # Filter pump control
    bestway_spa.py jets on|off        # Bubbles / wave jets
    bestway_spa.py hydrojet on|off    # HydroJet (if equipped)
    bestway_spa.py power on|off       # Master power
    bestway_spa.py raw                # Raw JSON device shadow
    bestway_spa.py debug              # Full API diagnostics

Credits:
    - M1R4G376/New_bestway_spa: Original V02 API reverse engineering
    - cdpuk/ha-bestway (PR #105 by ashleigh-hopkins): QR binding flow,
      AES encryption, device discovery via homes/rooms hierarchy
    - Bestway SmartSpa Android APK (decompiled): APPID, APPSECRET,
      AES IV, endpoint structure

License: MIT
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sys
import time
from pathlib import Path

import requests

# ── Constants ────────────────────────────────────────────────────────────────
# Extracted from decompiled Bestway SmartSpa Android APK.

APPID = "AhFLL54HnChhrxcl9ZUJL6QNfolTIB"
APPSECRET = "4ECvVs13enL5AiYSmscNjvlaisklQDz7vWPCCWXcEFjhWfTmLT"

API_HOSTS = {
    "EU": "smarthub-eu.bestwaycorp.com",
    "US": "smarthub-us.bestwaycorp.com",
    "CN": "smarthub.bestwaycorp.cn",
}

# AES-256-CBC IV from com.rongwei.library.utils.AESEncrypt in the APK.
FIXED_IV = bytes([56, 110, 58, 168, 76, 255, 94, 159, 237, 215, 171, 181, 150, 40, 74, 166])

# Default config path (next to this script, or override with BESTWAY_CONFIG env var)
DEFAULT_CONFIG = Path(__file__).parent / ".spa_config.json"

# Delay before read-back verification after commands (seconds)
READBACK_DELAY = 2.5

# Temperature limits (Celsius). The spa hardware enforces these too,
# but catching them client-side gives a better error message.
TEMP_MIN_C = 20
TEMP_MAX_C = 40
TEMP_MIN_F = 68
TEMP_MAX_F = 104


# ── HTTP Layer ───────────────────────────────────────────────────────────────

class BestwayAPI:
    """Low-level HTTP client for the Bestway SmartHub cloud API.

    NOTE: SSL verification is disabled (verify=False) because the Bestway API
    servers use certificates that fail validation in some environments. The API
    traffic is still encrypted via TLS; we just skip certificate chain verification.
    This matches the behavior of the official Android app (okhttp with relaxed SSL).
    """

    def __init__(self, host: str):
        self.host = host
        self.base_url = f"https://{host}"

    def _sign(self) -> tuple[str, str, str]:
        """Generate nonce, timestamp, and MD5 signature for request auth."""
        nonce = secrets.token_hex(16)
        ts = str(int(time.time()))
        sign = hashlib.md5(
            (APPID + APPSECRET + nonce + ts).encode()
        ).hexdigest().upper()
        return nonce, ts, sign

    def _headers(self, token: str | None = None) -> tuple[dict[str, str], str]:
        """Build signed headers. Returns (headers, sign).

        Sign is needed for AES encryption of v2 commands.
        """
        nonce, ts, sign = self._sign()
        headers = {
            "pushtype": "fcm",
            "appid": APPID,
            "nonce": nonce,
            "ts": ts,
            "accept-language": "en",
            "sign": sign,
            "Authorization": f"token {token}" if token else "token",
            "Host": self.host,
            "Connection": "Keep-Alive",
            "User-Agent": "okhttp/4.9.0",
            "Content-Type": "application/json; charset=UTF-8",
        }
        return headers, sign

    @staticmethod
    def _parse_response(resp: requests.Response) -> dict:
        """Parse JSON response, raising a clear error on non-JSON responses."""
        try:
            return resp.json()
        except (ValueError, requests.exceptions.JSONDecodeError):
            raise RuntimeError(
                f"API returned non-JSON (HTTP {resp.status_code}): {resp.text[:200]}"
            )

    def post(self, path: str, payload: dict, token: str | None = None) -> dict:
        headers, _ = self._headers(token)
        resp = requests.post(
            f"{self.base_url}{path}", headers=headers, json=payload,
            verify=False, timeout=15,
        )
        return self._parse_response(resp)

    def post_raw(self, path: str, payload: dict, headers: dict) -> dict:
        """POST with caller-provided headers (for grant_device)."""
        resp = requests.post(
            f"{self.base_url}{path}", headers=headers, json=payload,
            verify=False, timeout=15,
        )
        return self._parse_response(resp)

    def get(self, path: str, token: str) -> dict:
        headers, _ = self._headers(token)
        resp = requests.get(
            f"{self.base_url}{path}", headers=headers,
            verify=False, timeout=15,
        )
        return self._parse_response(resp)


# ── Config ───────────────────────────────────────────────────────────────────

def config_path() -> Path:
    import os
    return Path(os.environ.get("BESTWAY_CONFIG", DEFAULT_CONFIG))


def load_config() -> dict | None:
    p = config_path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            print(f"  Config file corrupt: {p}")
            print("  Delete it and run: bestway_spa.py setup")
            sys.exit(1)
    return None


def save_config(cfg: dict) -> None:
    p = config_path()
    p.write_text(json.dumps(cfg, indent=2) + "\n")
    print(f"  Config saved to {p}")


# ── Authentication ───────────────────────────────────────────────────────────

def authenticate(api: BestwayAPI, visitor_id: str, location: str = "GB") -> str | None:
    """Register/re-authenticate a visitor. Returns token or None.

    The visitor endpoint requires two undocumented fields (brand, marketing_notification)
    that cause HTTP 500 if missing. Discovered through systematic field testing.
    """
    payload = {
        "app_id": APPID,
        "brand": "",                    # Required but undocumented
        "lan_code": "en",
        "location": location,
        "marketing_notification": 0,    # Required but undocumented
        "push_type": "fcm",
        "timezone": "GMT",
        "visitor_id": visitor_id,
        "registration_id": "",
        "client_id": secrets.token_urlsafe(11)[:15],
    }
    result = api.post("/api/enduser/visitor", payload)
    token = result.get("data", {}).get("token")
    if not token:
        code = result.get("code", "?")
        msg = result.get("message", "unknown error")
        print(f"  Auth failed: code={code} — {msg}")
    return token


# ── AES Encryption ───────────────────────────────────────────────────────────

def encrypt_command(sign: str, plaintext: str) -> str | None:
    """AES-256-CBC encrypt a command payload.

    Key derivation: SHA-256("{sign},{APPSECRET}")[:32] as UTF-8 bytes.
    Output: Base64(FIXED_IV + ciphertext).

    Returns None if pycryptodome is not installed (falls back to v1 API).
    """
    try:
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import pad
    except ImportError:
        return None

    key_hex = hashlib.sha256(f"{sign},{APPSECRET}".encode()).hexdigest()[:32]
    key = key_hex.encode("utf-8")

    cipher = AES.new(key, AES.MODE_CBC, FIXED_IV)
    ciphertext = cipher.encrypt(pad(plaintext.encode(), AES.block_size))
    return base64.b64encode(FIXED_IV + ciphertext).decode()


# ── Device Binding ───────────────────────────────────────────────────────────

def bind_qr_code(api: BestwayAPI, token: str, qr_code: str) -> dict:
    """Bind a device to this visitor using a Share Device QR code.

    The full QR text (e.g. RW_Share_<id>_<device_id>) is the vercode.
    Flow from cdpuk/ha-bestway PR #105 by ashleigh-hopkins.
    """
    nonce = secrets.token_hex(16)
    ts = str(int(time.time()))
    sign = hashlib.md5((APPID + APPSECRET + nonce + ts).encode()).hexdigest().upper()

    headers = {
        "pushtype": "android",
        "appid": APPID,
        "nonce": nonce,
        "ts": ts,
        "sign": sign,
        "Authorization": f"token {token}",
        "Content-Type": "application/json; charset=UTF-8",
    }
    payload = {"vercode": qr_code, "push_type": "android"}
    return api.post_raw("/api/enduser/grant_device", payload, headers)


def discover_devices(api: BestwayAPI, token: str) -> list[dict]:
    """Walk homes -> rooms -> devices to find all bound devices.

    Discovery hierarchy from cdpuk/ha-bestway PR #105.
    """
    homes_resp = api.get("/api/enduser/homes", token)
    if homes_resp.get("code") != 0:
        return []

    devices = []
    for home in homes_resp.get("data", {}).get("list", []):
        home_id = home.get("id")
        if home_id is None:
            continue
        rooms_resp = api.get(f"/api/enduser/home/rooms?home_id={home_id}", token)
        if rooms_resp.get("code") != 0:
            continue
        for room in rooms_resp.get("data", {}).get("list", []):
            room_id = room.get("id")
            if room_id is None:
                continue
            devs_resp = api.get(
                f"/api/enduser/home/room/devices?room_id={room_id}", token,
            )
            if devs_resp.get("code") != 0:
                continue
            devices.extend(devs_resp.get("data", {}).get("list", []))
    return devices


# ── Device State ─────────────────────────────────────────────────────────────

def read_state(api: BestwayAPI, token: str, device_id: str, product_id: str) -> dict | None:
    """Read device shadow (thing_shadow). Returns reported state dict or None."""
    result = api.post("/api/device/thing_shadow/", {
        "device_id": device_id,
        "product_id": product_id,
    }, token)
    data = result.get("data", {})
    if "state" in data:
        return data["state"].get("reported") or data["state"].get("desired") or data["state"]
    return data or None


def print_state(state: dict | None) -> None:
    if not state:
        print("  No state data.")
        return

    online = state.get("is_online", 0)
    power = state.get("power_state", 0)
    heater = state.get("heater_state", 0)
    filt = state.get("filter_state", 0)
    waves = state.get("wave_state", 0)
    jets = state.get("hydrojet_state", 0)
    unit = "F" if state.get("temperature_unit", 1) == 0 else "C"
    current = state.get("water_temperature", "?")
    target = state.get("temperature_setting", "?")
    warning = state.get("warning", "")
    error = state.get("error_code", "")

    heater_map = {0: "OFF", 1: "Starting", 2: "Starting", 3: "Heating", 4: "At Target"}
    heater_str = heater_map.get(heater, f"Active ({heater})" if heater else "OFF")

    w = 39
    print()
    print(f"  +{'-' * w}+")
    print(f"  | {'Bestway Spa':^{w - 2}} |")
    print(f"  +{'-' * w}+")
    print(f"  |  Online:      {('YES' if online else 'NO'):>{w - 17}} |")
    print(f"  |  Power:       {('ON' if power else 'OFF'):>{w - 17}} |")
    print(f"  |  Water:       {(str(current) + unit):>{w - 17}} |")
    print(f"  |  Target:      {(str(target) + unit):>{w - 17}} |")
    print(f"  |  Heater:      {heater_str:>{w - 17}} |")
    print(f"  |  Filter:      {('ON' if filt else 'OFF'):>{w - 17}} |")
    print(f"  |  Bubbles:     {('ON' if waves else 'OFF'):>{w - 17}} |")
    if jets:
        print(f"  |  HydroJet:    {('ON' if jets else 'OFF'):>{w - 17}} |")
    if warning:
        print(f"  |  Warning:     {str(warning):>{w - 17}} |")
    if error:
        print(f"  |  Error:       {str(error):>{w - 17}} |")
    print(f"  +{'-' * w}+")
    print()


# ── Commands ─────────────────────────────────────────────────────────────────

def send_command(
    api: BestwayAPI, token: str, device_id: str, product_id: str,
    key: str, value: int,
) -> dict:
    """Send a command, trying v2 (AES encrypted) then falling back to v1.

    v2 endpoint: /api/v2/device/command (encrypted payload)
    v1 endpoint: /api/device/command/ (plaintext, may be deprecated)
    """
    shadow = {"state": {"desired": {key: value}}}
    desired_json = json.dumps(shadow, separators=(",", ":"))

    cmd = {"device_id": device_id, "product_id": product_id, "desired": desired_json}
    plaintext = json.dumps(cmd, separators=(",", ":"))

    # Try v2 encrypted
    headers, sign = api._headers(token)
    encrypted = encrypt_command(sign, plaintext)
    if encrypted:
        resp = requests.post(
            f"{api.base_url}/api/v2/device/command",
            headers=headers, json={"encrypted_data": encrypted},
            verify=False, timeout=15,
        )
        result = BestwayAPI._parse_response(resp)
        if result.get("code") == 0:
            return result

    # v1 fallback
    return api.post("/api/device/command/", {
        "device_id": device_id,
        "product_id": product_id,
        "desired": {"state": {"desired": {key: value}}},
    }, token)


def send_and_verify(
    api: BestwayAPI, token: str, device_id: str, product_id: str,
    key: str, value: int, label: str,
) -> None:
    """Send command, wait, read back state to confirm."""
    result = send_command(api, token, device_id, product_id, key, value)
    code = result.get("code", "?")
    if code != 0:
        print(f"  {label}: API returned code={code} msg={result.get('message', '?')}")
        return

    print(f"  {label}: command sent (code=0)")

    # Read-back verification
    time.sleep(READBACK_DELAY)
    state = read_state(api, token, device_id, product_id)
    if state:
        actual = state.get(key, "?")
        print(f"  Verified: {key}={actual}")
    else:
        print("  Could not verify (no state returned)")


# ── Setup ────────────────────────────────────────────────────────────────────

def setup(region: str | None = None) -> None:
    print("=" * 60)
    print("  bestway-spa-v2 Setup")
    print("=" * 60)

    # Region selection
    if not region:
        print("\n  Regions: EU, US, CN")
        region = input("  Your region [EU]: ").strip().upper() or "EU"

    if region not in API_HOSTS:
        print(f"  Unknown region '{region}'. Use EU, US, or CN.")
        sys.exit(1)

    api = BestwayAPI(API_HOSTS[region])

    # Authenticate
    print(f"\n[1/3] Registering visitor ({region})...")
    visitor_id = secrets.token_hex(8)
    token = authenticate(api, visitor_id)
    if not token:
        sys.exit(1)
    print(f"  OK — visitor_id: {visitor_id}")

    # QR code binding
    print("\n[2/3] Bind your spa via QR code")
    print("-" * 50)
    print("  In the Bestway SmartSpa app:")
    print("    1. Tap '...' on your spa")
    print("    2. Tap 'Share The Device'")
    print("    3. Scan the QR with your phone camera")
    print("    4. Copy the text (starts with RW_Share_)")
    print("    5. Paste it here within 15 minutes")
    print("-" * 50)

    qr = input("\n  QR code text: ").strip()
    if not qr:
        print("  Aborted.")
        sys.exit(1)

    if not qr.startswith("RW_Share_"):
        print(f"  Warning: expected RW_Share_ prefix")

    result = bind_qr_code(api, token, qr)
    if result.get("code") != 0:
        print(f"  Binding failed: {result.get('message', json.dumps(result))}")
        print("  QR may be expired or already used. Generate a new one.")
        sys.exit(1)

    bind_data = result.get("data", {})
    device_name = bind_data.get("name", "Spa")
    device_id = bind_data.get("device_id", "")
    print(f"  Bound: {device_name} ({device_id[:16]}...)")

    # Discover devices for product_id
    print("\n[3/3] Discovering device details...")
    devices = discover_devices(api, token)

    cfg = {
        "region": region,
        "visitor_id": visitor_id,
        "device_id": device_id,
        "device_name": device_name,
    }

    for dev in devices:
        if dev.get("device_id") == device_id:
            cfg["product_id"] = dev.get("product_id", "")
            cfg["product_series"] = dev.get("product_series", "")
            break
    else:
        # Take first device if exact match not found
        if devices:
            dev = devices[0]
            cfg["device_id"] = dev.get("device_id", device_id)
            cfg["product_id"] = dev.get("product_id", "")
            cfg["product_series"] = dev.get("product_series", "")

    if not cfg.get("product_id"):
        print("  Warning: could not determine product_id")
        print("  Raw device list:")
        for d in devices:
            print(f"    {json.dumps(d)}")
        sys.exit(1)

    save_config(cfg)

    # Verify
    print("\n  Verifying connection...")
    state = read_state(api, token, cfg["device_id"], cfg["product_id"])
    if state and state.get("water_temperature") is not None:
        print_state(state)
        print("  Setup complete!")
    else:
        print("  Connected but no state yet. Try: bestway_spa.py status")


# ── CLI ──────────────────────────────────────────────────────────────────────

def get_api_and_token() -> tuple[BestwayAPI, str, dict]:
    """Load config, authenticate, return (api, token, config)."""
    cfg = load_config()
    if not cfg:
        print("Not set up yet. Run: bestway_spa.py setup")
        sys.exit(1)
    if not cfg.get("product_id"):
        print("Missing product_id. Run: bestway_spa.py setup")
        sys.exit(1)

    region = cfg.get("region", "EU")
    if region not in API_HOSTS:
        print(f"Invalid region '{region}' in config. Run: bestway_spa.py setup")
        sys.exit(1)
    api = BestwayAPI(API_HOSTS[region])
    token = authenticate(api, cfg["visitor_id"])
    if not token:
        print("Auth failed. Try: bestway_spa.py setup")
        sys.exit(1)

    return api, token, cfg


def main() -> None:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1].lower()
    arg = sys.argv[2].lower() if len(sys.argv) > 2 else None

    if cmd == "setup":
        setup(region=arg.upper() if arg else None)
        return

    if cmd == "status":
        api, token, cfg = get_api_and_token()
        state = read_state(api, token, cfg["device_id"], cfg["product_id"])
        print_state(state)
        return

    if cmd == "raw":
        api, token, cfg = get_api_and_token()
        result = api.post("/api/device/thing_shadow/", {
            "device_id": cfg["device_id"], "product_id": cfg["product_id"],
        }, token)
        print(json.dumps(result, indent=2))
        return

    if cmd == "debug":
        api, token, cfg = get_api_and_token()
        vid = cfg['visitor_id']
        did = cfg['device_id']
        print(f"Region: {cfg.get('region')}")
        print(f"Visitor: {vid[:6]}...{vid[-4:]}")
        print(f"Device: {did[:6]}...{did[-4:]}")
        print(f"Product: {cfg.get('product_id')}")

        print("\n--- Homes ---")
        print(json.dumps(api.get("/api/enduser/homes", token), indent=2))

        print("\n--- Devices ---")
        for dev in discover_devices(api, token):
            print(json.dumps(dev, indent=2))

        print("\n--- Shadow ---")
        print(json.dumps(api.post("/api/device/thing_shadow/", {
            "device_id": cfg["device_id"], "product_id": cfg["product_id"],
        }, token), indent=2))
        return

    # Command dispatch
    api, token, cfg = get_api_and_token()
    did, pid = cfg["device_id"], cfg["product_id"]

    def parse_temp(a: str) -> int:
        try:
            t = int(a)
        except ValueError:
            print(f"  Invalid temperature: '{a}' (must be a number)")
            sys.exit(1)
        # Accept both C and F ranges
        if TEMP_MIN_C <= t <= TEMP_MAX_C or TEMP_MIN_F <= t <= TEMP_MAX_F:
            return t
        print(f"  Temperature {t} out of range.")
        print(f"  Valid: {TEMP_MIN_C}-{TEMP_MAX_C}C or {TEMP_MIN_F}-{TEMP_MAX_F}F")
        sys.exit(1)

    commands = {
        "heat":     ("heater_state",        lambda a: 1 if a in ("on", "1") else 0),
        "heater":   ("heater_state",        lambda a: 1 if a in ("on", "1") else 0),
        "temp":     ("temperature_setting",  parse_temp),
        "temperature": ("temperature_setting", parse_temp),
        "filter":   ("filter_state",        lambda a: 1 if a in ("on", "1") else 0),
        "jets":     ("wave_state",          lambda a: 100 if a in ("on", "1", "high") else 0),
        "bubbles":  ("wave_state",          lambda a: 100 if a in ("on", "1", "high") else 0),
        "waves":    ("wave_state",          lambda a: 100 if a in ("on", "1", "high") else 0),
        "hydrojet": ("hydrojet_state",      lambda a: 1 if a in ("on", "1") else 0),
        "hydro":    ("hydrojet_state",      lambda a: 1 if a in ("on", "1") else 0),
        "power":    ("power_state",         lambda a: 1 if a in ("on", "1") else 0),
    }

    if cmd not in commands:
        print(f"Unknown command: {cmd}")
        print("Commands: setup, status, heat, temp, filter, jets, hydrojet, power, raw, debug")
        sys.exit(1)

    if arg is None:
        print(f"Usage: bestway_spa.py {cmd} <value>")
        sys.exit(1)

    key, parse = commands[cmd]
    value = parse(arg)
    label = f"{cmd} {'on' if value else 'off'}" if key != "temperature_setting" else f"temp {value}"
    send_and_verify(api, token, did, pid, key, value, label)


if __name__ == "__main__":
    try:
        main()
    except requests.exceptions.ConnectionError:
        print("  Connection failed. Check your network and try again.")
        sys.exit(1)
    except requests.exceptions.Timeout:
        print("  Request timed out. The Bestway API may be slow or down.")
        sys.exit(1)
    except RuntimeError as e:
        print(f"  Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n  Aborted.")
        sys.exit(130)
