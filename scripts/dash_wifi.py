#!/usr/bin/env python3
"""Joins the bike's dash Wi-Fi (RE_*) while it is in range; otherwise keeps the Pi on its
normal Wi-Fi so SSH keeps working.

The Pi Zero 2 W has one 2.4 GHz radio, so being on the dash means being off the home
network. Safety rules, so a mistake here never strands the Pi:
  - Existing Wi-Fi profiles are never edited or deleted. This script only owns the
    "wayfindr-dash" profile, which has autoconnect OFF: NetworkManager on its own never
    picks the dash, so stopping this service always leaves the Pi on its usual networks.
  - A failed dash join hands the radio straight back to NetworkManager's normal choice
    (`nmcli device connect`), then backs off before trying the dash again.
  - When the dash goes away the Pi is sent back to its usual network explicitly.
  - If the Pi sits with no Wi-Fi at all for several minutes, NetworkManager is restarted.
  - Kill switch: while /boot/firmware/wayfindr-no-dash (creatable from any PC with the SD
    card in a reader) or /home/*/wayfindr-no-dash exists, the dash is never joined.

The exact dash SSID is written to /run/wayfindr/dash-ssid while connected; the dash
checks it inside the auth handshake, so the bridge needs it.
"""

import glob
import os
import re
import subprocess
import sys
import time

IFACE = os.environ.get("WIFI_IFACE", "wlan0")
PREFIX = os.environ.get("DASH_PREFIX", "RE_")
PSK = os.environ.get("DASH_PSK", "12345678")
PROFILE = "wayfindr-dash"
BRIDGE_UNIT = "wayfindr-dash-bridge.service"
STATE_DIR = "/run/wayfindr"
SSID_FILE = os.path.join(STATE_DIR, "dash-ssid")
KILL_SWITCHES = ["/boot/firmware/wayfindr-no-dash", "/home/*/wayfindr-no-dash"]

SCAN_EVERY_S = 15
ON_DASH_POLL_S = 3
JOIN_WAIT_S = 25
BACKOFF_MIN_S = 30
BACKOFF_MAX_S = 600
OFFLINE_RECONNECT_S = 30     # no Wi-Fi at all for this long: ask NM to reconnect
OFFLINE_RESTART_NM_S = 300   # still nothing: restart NetworkManager


def log(msg):
    print(msg, flush=True)


def nm(*args, timeout=40):
    try:
        p = subprocess.run(["nmcli", *args], capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, "timeout"


def kill_switch():
    return any(glob.glob(pattern) for pattern in KILL_SWITCHES)


def active_connection():
    rc, out = nm("-g", "GENERAL.CONNECTION", "device", "show", IFACE, timeout=10)
    return out.splitlines()[0].strip() if rc == 0 and out else ""


def ipv4_address():
    rc, out = nm("-g", "IP4.ADDRESS", "device", "show", IFACE, timeout=10)
    return out.split("/")[0].strip() if rc == 0 and out else ""


def split_terse(line):
    """Split one `nmcli -t` line on unescaped colons."""
    return [f.replace("\\:", ":") for f in re.split(r"(?<!\\):", line)]


def scan_for_dash():
    """Dash SSIDs in range, strongest first."""
    nm("device", "wifi", "rescan", "ifname", IFACE, timeout=15)
    time.sleep(3)
    rc, out = nm("-t", "-f", "SSID,SIGNAL", "device", "wifi", "list", "ifname", IFACE, "--rescan", "no", timeout=15)
    if rc != 0:
        return []
    found = {}
    for line in out.splitlines():
        fields = split_terse(line)
        if len(fields) < 2:
            continue
        ssid, signal = fields[0], fields[1]
        if ssid.startswith(PREFIX):
            found[ssid] = max(found.get(ssid, 0), int(signal or 0))
    return sorted(found, key=found.get, reverse=True)


def ensure_profile(ssid):
    rc, _ = nm("-g", "connection.id", "connection", "show", PROFILE, timeout=10)
    settings = [
        "connection.interface-name", IFACE,
        "connection.autoconnect", "no",
        "802-11-wireless.ssid", ssid,
        "802-11-wireless.powersave", "2",   # off: power save adds latency to the video
        "wifi-sec.key-mgmt", "wpa-psk",
        "wifi-sec.psk", PSK,
        "ipv4.method", "auto",
        "ipv4.never-default", "yes",        # the dash has no internet
        "ipv6.method", "disabled",
    ]
    if rc == 0:
        nm("connection", "modify", PROFILE, *settings, timeout=15)
    else:
        nm("connection", "add", "type", "wifi", "con-name", PROFILE, *settings, timeout=15)


def back_to_usual_network(reason):
    log(f"returning to the usual Wi-Fi ({reason})")
    rc, out = nm("--wait", "30", "device", "connect", IFACE, timeout=45)
    log(f"  -> {active_connection() or 'not connected'} {ipv4_address()}" + ("" if rc == 0 else f" ({out})"))


def write_ssid(ssid):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = SSID_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(ssid)
    os.replace(tmp, SSID_FILE)


def clear_ssid():
    try:
        os.remove(SSID_FILE)
    except FileNotFoundError:
        pass


def main():
    log(f"dash Wi-Fi manager: prefix {PREFIX!r} on {IFACE}; kill switch {KILL_SWITCHES[0]}")
    clear_ssid()
    failures = 0
    next_try = 0.0
    offline_since = None
    nm_restarted = False

    while True:
        active = active_connection()

        if active == PROFILE:
            offline_since = None
            if kill_switch():
                clear_ssid()
                nm("connection", "down", PROFILE, timeout=20)
                back_to_usual_network("kill switch present")
                continue
            rc, ssid = nm("-g", "802-11-wireless.ssid", "connection", "show", PROFILE, timeout=10)
            if rc == 0 and ssid:
                write_ssid(ssid.strip())
            time.sleep(ON_DASH_POLL_S)
            continue

        if os.path.exists(SSID_FILE):
            clear_ssid()
            log("left the dash Wi-Fi")
            if not active:
                back_to_usual_network("dash gone")
                continue

        if not active:
            offline_since = offline_since or time.monotonic()
            offline = time.monotonic() - offline_since
            if offline >= OFFLINE_RESTART_NM_S and not nm_restarted:
                log(f"no Wi-Fi for {offline:.0f} s; restarting NetworkManager")
                subprocess.run(["systemctl", "restart", "NetworkManager"], timeout=60)
                nm_restarted = True
                time.sleep(20)
                continue
            if offline >= OFFLINE_RECONNECT_S:
                back_to_usual_network(f"offline {offline:.0f} s")
        else:
            offline_since = None
            nm_restarted = False

        if not kill_switch() and time.monotonic() >= next_try:
            dashes = scan_for_dash()
            if dashes:
                ssid = dashes[0]
                log(f"dash in range: {ssid}; joining (was on {active or 'nothing'})")
                ensure_profile(ssid)
                rc, out = nm("--wait", str(JOIN_WAIT_S), "connection", "up", PROFILE, timeout=JOIN_WAIT_S + 15)
                if rc == 0 and active_connection() == PROFILE:
                    log(f"joined {ssid}, address {ipv4_address()}")
                    write_ssid(ssid)
                    # The bridge must be running to stream; start it in case it was left stopped
                    # (no-op if it already runs).
                    if subprocess.run(["systemctl", "is-active", "--quiet", BRIDGE_UNIT]).returncode != 0:
                        log(f"{BRIDGE_UNIT} was not running; starting it")
                        subprocess.run(["systemctl", "start", BRIDGE_UNIT], timeout=30)
                    failures = 0
                    continue
                failures += 1
                backoff = min(BACKOFF_MIN_S * 2 ** (failures - 1), BACKOFF_MAX_S)
                next_try = time.monotonic() + backoff
                reason = next((l for l in out.splitlines() if "rror" in l), out.splitlines()[0] if out else str(rc))
                log(f"joining {ssid} failed ({reason}); retry in {backoff} s")
                back_to_usual_network("dash join failed")

        time.sleep(SCAN_EVERY_S)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
