#!/usr/bin/env python3
"""Forwards Android Auto video from the phone (through the wireless AA dongle and
aa-headless) to the bike's dash, using Wayfindr's dash protocol.

Runs whenever dash_wifi.py has the Pi on the dash Wi-Fi (/run/wayfindr/dash-ssid exists):
  1. K1G session, as in Wayfindr's DashSession.kt: initial burst, RSA/AES auth, nav-mode
     entry, then 4 Hz projection keep-alive, 1 Hz route card and 1 Hz status heartbeat,
     answering the dash's per-frame decode acks and button events.
  2. Video: aa-headless (AA H.264, 800x480 ~30 fps) -> ffmpeg (4 fps, 526x300, Baseline 4.1,
     ~200 kbps) -> NAL handling + RTP as in NalProcessor.kt / RtpPacketizer.kt -> 192.168.1.1:5000.
     While the phone isn't connected, a "waiting" card from a second ffmpeg keeps the
     dash fed so it doesn't time out.

Settings come from the environment (see /etc/default/wayfindr-pi).
"""

import glob
import os
import random
import select
import shutil
import socket
import subprocess
import sys
import threading
import time

import k1g

IFACE = os.environ.get("WIFI_IFACE", "wlan0")
SSID_FILE = "/run/wayfindr/dash-ssid"
AA_BIN = os.environ.get("AA_BIN", "/opt/wayfindr/hu/aa-headless")
AA_DPI = os.environ.get("AA_DPI", "140")
AA_ALWAYS = os.environ.get("AA_ALWAYS", "1") == "1"    # keep AA connected off the dash too
AA_LAUNCH = os.environ.get("AA_LAUNCH", "maps")        # app opened full screen at start: maps | media | phone | none
AA_STUCK_RUNS = 5         # this many aa-headless runs in a row that end quickly with no video ...
AA_STUCK_RUN_S = 15       # ... (each shorter than this) mean the dongle is stuck: reset its USB
AA_RESET_GAP_S = 120      # at most one reset this often
VIDEO_MODE = os.environ.get("VIDEO_MODE", "fill")      # fill | dome | circle | full (see aa_crop)
# Visible part of the dash's round screen, in 526x300 frame pixels: circle centre x, y and
# radius. Measured from a photo of the dash showing a stream (2026-10-31): the screen edge
# touches the frame's top row at the centre and is ~510 px wide at the frame's bottom edge.
DASH_CIRCLE = tuple(float(v) for v in os.environ.get("DASH_CIRCLE", "263,257,259").split(","))
DASH_EDGE_INSET = int(os.environ.get("DASH_EDGE_INSET", "6"))   # keep this far inside the edge
# Rows cut off the bottom of AA's 800x480 frame: its taskbar (clock + app icons) is the bottom
# 70 rows at dpi 140 (measured 2026-10-31). 0 keeps it.
VIDEO_CROP_BOTTOM = int(os.environ.get("VIDEO_CROP_BOTTOM", "72"))
VIDEO_FPS = os.environ.get("VIDEO_FPS", "4")
VIDEO_BITRATE = os.environ.get("VIDEO_BITRATE", "200k")
VIDEO_PRESET = os.environ.get("VIDEO_PRESET", "medium")   # x264 preset; ultrafast if the Pi falls behind
TITLE = os.environ.get("DASH_TITLE", "Android Auto")
# Driver seat side set on the phone (Android Auto > Advanced > Driver seat location). AA mirrors
# its whole layout with it, so it must match: left = side bar, map buttons and turn card on the
# left (measured 2026-10-31); right = the original layout. The Pi cannot change it.
AA_SEAT = os.environ.get("AA_SEAT", "right")
LEFT_SEAT = AA_SEAT == "left"
# Route card sent while streaming. "turn" = Wayfindr's proven card (the dash draws a "depart"
# turn circle); with it the dash reported joystick moves on the bike (2026-10-31). "none" = the
# experimental no-turn card (exp-dash-no-turn-idle; symbol, units and ETA all blank): the dash
# then sent no left/right/down presses. "nosymbol" = the proven card with only the turn symbol
# blanked: no circle, and again no joystick (2026-10-31), so the joystick needs a turn symbol.
# A hex code (e.g. "0x0B") = the proven card with that turn symbol, for trying other codes.
IDLE_CARD = os.environ.get("DASH_IDLE_CARD", "turn")


def route_card(projection_on):
    if IDLE_CARD == "none":
        return k1g.idle_route_card(TITLE, projection_on)
    if IDLE_CARD == "nosymbol":
        return k1g.route_card(TITLE, projection_on, maneuver=k1g.NAV_NONE)
    if IDLE_CARD.startswith("0x"):
        return k1g.route_card(TITLE, projection_on, maneuver=int(IDLE_CARD, 16))
    return k1g.route_card(TITLE, projection_on)


HOSTNAME = os.environ.get("DASH_HOSTNAME", "WayfindR")

DASH_IP = "192.168.1.1"
BROADCAST = "192.168.1.255"
CTRL_PORT, RX_PORT, RTP_PORT = 2000, 2002, 5000
WIDTH, HEIGHT = 526, 300

AUTH_TIMEOUT_S = 15
RX_WATCHDOG_S = 6
FILLER_AFTER_S = 1.5     # no AA frame for this long: switch the dash to the waiting card


def log(msg):
    print(msg, flush=True)


def dash_ssid():
    try:
        with open(SSID_FILE) as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


# ── RTP (RtpPacketizer.kt) ────────────────────────────────────────────────────

class RtpPacketizer:
    """RFC 6184 for the dash: single NAL or FU-A (never STAP-A), marker on the last packet of
    each NAL, 90 kHz clock, payload <= 1380 bytes."""
    MAX_PAYLOAD = 1380
    PT = 96

    def __init__(self, send):
        self.send = send
        self.seq = random.randrange(0x10000)
        self.ssrc = random.getrandbits(32)
        self.ts_base = random.getrandbits(32)

    def packetize(self, nal, pts_ms):
        ts = (self.ts_base + pts_ms * 90) & 0xFFFFFFFF
        if len(nal) <= self.MAX_PAYLOAD:
            self._emit(nal, True, ts)
            return
        nal_type = nal[0] & 0x1F
        fu_ind = (nal[0] & 0xE0) | 28
        offset = 1
        first = True
        while offset < len(nal):
            chunk = nal[offset:offset + self.MAX_PAYLOAD - 2]
            last = offset + len(chunk) >= len(nal)
            fu_hdr = (0x80 if first else 0) | (0x40 if last else 0) | nal_type
            self._emit(bytes([fu_ind, fu_hdr]) + chunk, last, ts)
            offset += len(chunk)
            first = False

    def _emit(self, payload, marker, ts):
        hdr = bytes([0x80, (0x80 if marker else 0) | self.PT]) + self.seq.to_bytes(2, "big") + \
            ts.to_bytes(4, "big") + self.ssrc.to_bytes(4, "big")
        self.seq = (self.seq + 1) & 0xFFFF
        self.send(hdr + payload)


# ── NAL handling (NalProcessor.kt) ────────────────────────────────────────────

def split_nals(data):
    """Annex-B byte stream -> NAL units (start codes removed)."""
    codes = []   # (start code begin, payload begin); a 4-byte code is 00 00 00 01
    i = 0
    while (j := data.find(b"\0\0\1", i)) >= 0:
        codes.append((j - 1 if j > 0 and data[j - 1] == 0 else j, j + 3))
        i = j + 3
    nals = []
    for k, (_, begin) in enumerate(codes):
        end = codes[k + 1][0] if k + 1 < len(codes) else len(data)
        if end > begin:
            nals.append(data[begin:end])
    return nals


class NalProcessor:
    """Per video source: cache SPS/PPS (SPS normalised to the stock phone's 67 42 00 29 shape
    the dash firmware whitelists), bundle them in front of every IDR, drop SEI/AUD."""

    def __init__(self):
        self.sps = None
        self.pps = None

    def frame_units(self, data):
        """One encoder output buffer -> list of (unit bytes, is_idr) to send."""
        out = []
        for nal in split_nals(data):
            t = nal[0] & 0x1F
            if t == 7:
                sps = bytearray(nal)
                if len(sps) >= 4 and sps[1] == 0x42:
                    sps[2] = 0x00
                    sps[3] = 0x29
                self.sps = bytes(sps)
            elif t == 8:
                self.pps = nal
            elif t == 5:
                if self.sps and self.pps:
                    out.append((self.sps + b"\0\0\0\1" + self.pps + b"\0\0\0\1" + nal, True))
                else:
                    out.append((nal, True))
            elif t in (6, 9):
                continue
            elif 1 <= t <= 4 or 10 <= t <= 12:
                out.append((nal, False))
        return out


# ── Video sources ─────────────────────────────────────────────────────────────

# Colour properties stamped on every frame. x264 writes them into the SPS, so with these (and
# setsar=1) the AA and filler encoders produce byte-identical SPS: the dash never sees its
# stream parameters change when the bridge switches source.
FRAME_PARAMS = "setparams=range=tv:colorspace=bt709:color_primaries=bt709:color_trc=bt709"


def x264_args():
    """Same encoder settings for both sources. At 4 fps and ~400x240 the encoder has time to
    spare, so a slower preset buys a much cleaner picture at the same bitrate (SSIM 0.941 with
    ultrafast vs 0.966 with medium on captured Maps footage). One reference frame, as the dash
    has only been fed single-reference streams; it costs no quality here."""
    fps = int(VIDEO_FPS)
    return ["-c:v", "libx264", "-preset", VIDEO_PRESET, "-tune", "zerolatency", "-refs", "1",
            "-profile:v", "baseline", "-level", "4.1", "-pix_fmt", "yuv420p",
            "-b:v", VIDEO_BITRATE, "-maxrate", VIDEO_BITRATE, "-bufsize", "100k",
            "-g", str(fps), "-keyint_min", str(fps), "-sc_threshold", "0", "-bf", "0",
            "-x264-params", "repeat-headers=1:sliced-threads=0", "-threads", "1",
            "-flush_packets", "1", "-f", "h264", "pipe:1"]


# AA screen area per mode, as the "WxH" margin aa-headless asks for (AA lays out its UI in the
# centred (800-W)x(480-H) part of the frame).
AA_MARGINS = {"fill": "240x180", "dome": "240x180", "circle": "320", "full": "0"}


def aa_crop():
    """(w, h, x, y) of the part of AA's 800x480 frame sent to the dash.
    dome:   AA lays out 560x300. That is short enough that AA moves its taskbar to a rail on the
            right (70 px at dpi 140), and narrow enough that Maps centres the turn card over the
            map. The map is the 490x300 left of the rail (measured 2026-10-31).
    circle: AA lays out a 480x480 square, card top centre; taskbar rows cut off the bottom.
    full:   AA's whole width, taskbar cut off, sides trimmed to fill 526x300 unstretched.
    fill:   the dome layout at AA_DPI 110: AA sees a big enough screen that Maps shows its
            + / - buttons (the joystick taps them), but moves the turn card to the top right.
            The map plus button column is 500x300 left of the rail (measured 2026-10-31); it
            is placed by fill_placement() so the card and buttons stay inside the circle."""
    if VIDEO_MODE == "fill":
        # map + button column: right of the side bar (left seat) or left of it (right seat)
        return (504, 300, 176, 90) if LEFT_SEAT else (500, 300, 120, 90)
    if VIDEO_MODE == "dome":
        return 490, 300, 120, 90
    h = (480 - VIDEO_CROP_BOTTOM) // 2 * 2
    if VIDEO_MODE == "circle":
        return 480, h, 160, 0
    w = min(800, int(h * WIDTH / HEIGHT) // 2 * 2)
    return w, h, (800 - w) // 2, 0


# Where Maps' turn card sits inside the dome map (AA pixels of the 490x300 map, measured
# 2026-10-31): 34 px in from each side, 8 px down from the top. Its top corners are what must
# stay on screen; the map's own top corners are only map imagery and may be clipped.
DOME_CARD_INSET_X = 34
DOME_CARD_INSET_TOP = 8


def _circle_half_width(y):
    cx, cy, r = DASH_CIRCLE
    dy = cy - y
    return (r * r - dy * dy) ** 0.5 if r > abs(dy) else 0.0


def dome_placement(src_w, src_h):
    """(w, h, top) for the map, sitting on the frame's bottom edge: as large as possible while
    the turn card's top corners and the map's bottom corners stay inside the visible circle."""
    for top in range(0, HEIGHT - 20, 2):
        h = HEIGHT - top
        w = min(WIDTH, int(h * src_w / src_h) // 2 * 2)
        s = w / src_w
        card_half = w / 2 - DOME_CARD_INSET_X * s
        card_top = top + DOME_CARD_INSET_TOP * s
        if (_circle_half_width(card_top) - DASH_EDGE_INSET >= card_half and
                _circle_half_width(HEIGHT - 1) - DASH_EDGE_INSET >= w / 2):
            return w, h, top
    return WIDTH // 2, HEIGHT // 2, HEIGHT // 2


# Points of the fill map (AA pixels of the 500x300 crop, measured 2026-10-31) that must stay
# inside the visible circle: the turn/search card's top corners (x 215-448, 10 down; same in
# browse and navigation), the right edge of the + / - buttons, and the bottom-left corner.
FILL_KEEP = [(215, 10), (448, 10), (496, 203), (496, 288), (20, 288)]


# FILL_LAYOUT="scale,x,y" places the fill map by hand (the user chose 0.846,100,49 with the
# Dash Map Sizer on 2026-10-31: map clear of the dash's turn circle at about x 17-138,
# y 145-266). Empty = computed by fill_placement().
# Left seat: 0.876,87,41, chosen by the user in the Dash Map Sizer on 2026-11-01 (the zoom
# buttons sit under the dash turn circle; the joystick presses them, so they need not show).
FILL_LAYOUT = os.environ.get("FILL_LAYOUT", "0.876,87,41" if LEFT_SEAT else "0.846,100,49")
# Where the dash draws its own turn circle over our frame (centre x, centre y, diameter in
# 526x300 frame px), as the user lined it up in the Dash Map Sizer on 2026-11-01. Not used by
# the code; kept as the reference for future layouts.
DASH_TURN_CIRCLE = (77, 228, 139)


def fill_layout(src_w, src_h):
    if FILL_LAYOUT:
        s, x, y = FILL_LAYOUT.split(",")
        return float(s), int(x), int(y)
    return fill_placement(src_w, src_h)


def fill_placement(src_w, src_h):
    """(scale, x, y) for the fill map: as large as possible while every FILL_KEEP point stays
    inside the visible circle and the frame."""
    cx, cy, r = DASH_CIRCLE
    r -= DASH_EDGE_INSET
    for si in range(int(max(WIDTH / src_w, HEIGHT / src_h) * 1000), 500, -4):
        s = si / 1000
        for top in range(0, HEIGHT // 2, 2):
            lo, hi = -src_w * s, WIDTH
            for px, py in FILL_KEEP:
                y = py * s + top
                if y >= HEIGHT or (y - cy) ** 2 > r * r:
                    break
                half = (r * r - (y - cy) ** 2) ** 0.5
                lo = max(lo, cx - half - px * s, -px * s)
                hi = min(hi, cx + half - px * s, WIDTH - 1 - px * s)
            else:
                if lo <= hi:
                    return s, int((lo + hi) / 2) // 2 * 2, top
    return 0.5, 0, 0


# AA's rail at AA_DPI 110 (AA pixels of the 800x480 frame, measured 2026-10-31): signal and
# battery icons, then the clock under them. fill mode copies both into one strip, top centre
# above the turn card. DASH_STATUS=off leaves it out.
DASH_STATUS = os.environ.get("DASH_STATUS", "top")
RAIL_ICONS = (44, 18, 125, 102) if LEFT_SEAT else (44, 18, 630, 102)
RAIL_CLOCK = (52, 24, 121, 118) if LEFT_SEAT else (52, 24, 626, 118)
STATUS_TOP = 12


def status_strip():
    """Filter chains taking [st_in] to [st]: icons and clock side by side on black."""
    iw, ih, ix, iy = RAIL_ICONS
    cw, ch, cx, cy = RAIL_CLOCK
    h = ch + 2
    return (f"[st_in]split[ic][ck];"
            f"[ic]crop={iw}:{ih}:{ix}:{iy},pad={iw + 8}:{h}:6:{(h - ih) // 2 // 2 * 2}:black[icp];"
            f"[ck]crop={cw}:{ch}:{cx}:{cy},pad={cw + 4}:{h}:0:2:black[ckp];"
            f"[icp][ckp]hstack[st]")


def aa_filter():
    w, h, x, y = aa_crop()
    crop = f"crop={w}:{h}:{x}:{y}"
    if VIDEO_MODE == "fill":
        # The map is shrunk just enough to keep the turn card and zoom buttons inside the
        # circle; the space around it shows a blurred, dimmed copy of the map instead of black.
        s = max(WIDTH / w, HEIGHT / h)
        sw, sh = int(w * s + 1) // 2 * 2, int(h * s + 1) // 2 * 2
        k, mx, my = fill_layout(w, h)
        fit = (f"split[fg][bg];"
               f"[bg]scale={sw}:{sh},crop={WIDTH}:{HEIGHT}:{(sw - WIDTH) // 2}:{(sh - HEIGHT) // 2},"
               f"scale={WIDTH // 4}:{HEIGHT // 4},boxblur=2:1,scale={WIDTH}:{HEIGHT},"
               f"eq=brightness=-0.08[blur];"
               f"[fg]scale={int(w * k) // 2 * 2}:{int(h * k) // 2 * 2}:flags=lanczos[map];"
               f"[blur][map]overlay={mx}:{my}")
    elif VIDEO_MODE == "dome":
        # Pushed down clear of the curved top, as wide as the circle allows there.
        dw, dh, top = dome_placement(w, h)
        fit = f"scale={dw}:{dh}:flags=lanczos,pad={WIDTH}:{HEIGHT}:(ow-iw)/2:{top}"
    elif VIDEO_MODE == "circle":
        # Full height of the dash frame, centred, black at the sides (outside the round display).
        fit = f"scale={min(WIDTH, int(w * HEIGHT / h) // 2 * 2)}:{HEIGHT}:flags=lanczos,pad={WIDTH}:{HEIGHT}:(ow-iw)/2:0"
    else:
        fit = f"scale={WIDTH}:{HEIGHT}:flags=lanczos"
    if VIDEO_MODE == "fill" and DASH_STATUS == "top":
        # The rail is outside the crop, so it is split off the full AA frame first.
        return (f"fps={VIDEO_FPS},split[src][st_in];[src]{crop},{fit}[base];{status_strip()};"
                f"[base][st]overlay=(W-w)/2:{STATUS_TOP},setsar=1,{FRAME_PARAMS}")
    # setsar=1 and FRAME_PARAMS on every source keep the SPS identical (see x264_args).
    # The whole Pi pipeline holds a frame ~390 ms (latencytest.py). A select-based picker
    # instead of fps saved only ~65 ms and needs -fps_mode passthrough plus a pinned SPS rate.
    return f"fps={VIDEO_FPS},{crop},{fit},setsar=1,{FRAME_PARAMS}"


def filler_filter():
    font = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    text = ""
    if os.path.exists(font):
        text = (f",drawtext=fontfile={font}:text='Waiting for Android Auto':fontcolor=white:fontsize=22:"
                "x=(w-text_w)/2:y=(h-text_h)/2")
    return f"format=yuv420p{text},setsar=1,{FRAME_PARAMS}"


class Pipeline:
    """A process chain whose final stdout is an Annex-B H.264 stream at the dash format."""

    def __init__(self, name, procs):
        self.name = name
        self.procs = procs

    @property
    def stdout(self):
        return self.procs[-1].stdout

    def alive(self):
        return all(p.poll() is None for p in self.procs)

    def send_input(self, line):
        """One input command to aa-headless (the first process), if it takes input."""
        stdin = self.procs[0].stdin
        if stdin is None or self.procs[0].poll() is not None:
            return False
        try:
            stdin.write((line + "\n").encode())
            stdin.flush()
            return True
        except (BrokenPipeError, ValueError):
            return False

    def stop(self):
        for p in reversed(self.procs):
            if p.poll() is None:
                p.terminate()
        for p in self.procs:
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()


class NowPlaying:
    """What the phone is playing, from aa-headless's "media: ..." log lines (Android Auto's
    media status channel). The dash session forwards it to the dash's own music widget."""

    def __init__(self):
        self.lock = threading.Lock()
        self.title = self.artist = self.album = ""
        self.state = "none"     # play | pause | change | none

    def ingest(self, line):
        # aa-headless lines are "HH:MM:SS media: track ..." / "HH:MM:SS media: state ..."
        _, _, rest = line.partition(" media: ")
        """Returns whether anything changed."""
        with self.lock:
            before = (self.title, self.artist, self.album, self.state)
            if rest.startswith("track "):
                self.title, self.artist, self.album = (rest[6:].split("	") + ["", ""])[:3]
            elif rest.startswith("state "):
                self.state = rest[6:].strip()
            return (self.title, self.artist, self.album, self.state) != before

    def snapshot(self):
        with self.lock:
            return self.title, self.artist, self.album, self.state


now_playing = NowPlaying()


def drain_stderr(stream, prefix, keep):
    """Forward a child's stderr to our log, dropping noisy lines."""
    for raw in iter(stream.readline, b""):
        line = raw.decode(errors="replace").rstrip()
        if prefix == "aa" and " media: " in line:
            changed = now_playing.ingest(line)
            if not changed:
                continue    # the phone repeats the play state every second
        if line and keep(line):
            log(f"{prefix}: {line}")


# Strip of AA's frame (x, y, w, h) where Maps shows its "Re-centre" button once the map has
# been moved: x ~300-400 outside a route, ~225-315 during one (left of the route card, whose
# end-navigation X is at ~365). ffmpeg sends it to the bridge twice a second so the joystick
# taps Re-centre only where it is actually drawn (fill layout, AA_DPI 110).
# Left seat: Re-centre sits right of the ETA card (route ~529,358).
PROBE = (300, 340, 340, 36) if LEFT_SEAT else (190, 340, 240, 36)


class ScreenProbe:
    """Latest RGB copy of PROBE, read from ffmpeg's second output."""

    def __init__(self, fd):
        self.latest = None   # (monotonic time, bytes)
        threading.Thread(target=self._read, args=(fd,), daemon=True).start()

    def _read(self, fd):
        size = PROBE[2] * PROBE[3] * 3
        with os.fdopen(fd, "rb") as f:
            while (data := f.read(size)) and len(data) == size:
                self.latest = (time.monotonic(), data)

    def find_recentre(self, max_age=1.5):
        """AA frame point of Maps' Re-centre button, or None. Its label is the only teal text
        in the strip (light theme dark teal, dark theme light teal; both have red far below
        green and blue, which are close). Map water is bluer, parks greener, the route purple."""
        if not self.latest or time.monotonic() - self.latest[0] > max_age:
            return None
        data, (x0, y0, w, _) = self.latest[1], PROBE
        xs, ys = [], []
        for i in range(0, len(data), 3):
            r, g, b = data[i], data[i + 1], data[i + 2]
            if g - r > 50 and b - r > 50 and abs(g - b) < 30:
                xs.append(i // 3 % w)
                ys.append(i // 3 // w)
        # The label is ~280 matching pixels; scattered map colours reached 41 (route preview).
        if len(xs) < 150:
            return None
        return x0 + sum(xs) // len(xs), y0 + sum(ys) // len(ys)


def start_aa():
    # gvfs's MTP monitor grabs the dongle (its interface is named "MTP"); keep it off.
    subprocess.run(["pkill", "-x", "gvfsd-mtp"], stderr=subprocess.DEVNULL)
    margin = AA_MARGINS.get(VIDEO_MODE, "0")
    aa = subprocess.Popen([AA_BIN, "-", margin, AA_DPI, AA_LAUNCH],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    probe_r, probe_w = os.pipe()
    px, py, pw, ph = PROBE
    ff = subprocess.Popen(
        # Small probe: by default ffmpeg buffers up to 5 MB / 5 s of the pipe before decoding.
        # (Don't add -fflags nobuffer -flags low_delay: with them ffmpeg outputs nothing here.)
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-probesize", "32768", "-analyzeduration", "0",
         "-use_wallclock_as_timestamps", "1", "-f", "h264", "-threads", "2", "-i", "pipe:0",
         "-filter_complex", f"[0:v]split=2[m][p];[m]{aa_filter()}[v];"
                            f"[p]fps=2,crop={pw}:{ph}:{px}:{py},format=rgb24[s]",
         "-map", "[v]", *x264_args(),
         "-map", "[s]", "-f", "rawvideo", f"pipe:{probe_w}"],
        stdin=aa.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=(probe_w,))
    aa.stdout.close()
    os.close(probe_w)
    threading.Thread(target=drain_stderr, args=(aa.stderr, "aa", lambda l: not l.startswith("[")), daemon=True).start()
    threading.Thread(target=drain_stderr, args=(ff.stderr, "ffmpeg(aa)", lambda l: True), daemon=True).start()
    pipeline = Pipeline("aa", [aa, ff])
    pipeline.probe = ScreenProbe(probe_r)
    return pipeline


def reset_dongle():
    """Disconnects and reconnects the dongle's USB in software. Cut off mid-session, the dongle
    can stay stuck in accessory mode, failing every new session with aasdk error 26, until it is
    re-plugged; this does the re-plug."""
    for vid_file in glob.glob("/sys/bus/usb/devices/*/idVendor"):
        dev = os.path.dirname(vid_file)
        try:
            with open(vid_file) as f:
                vid = f.read().strip()
            with open(os.path.join(dev, "idProduct")) as f:
                pid = f.read().strip()
            if (vid, pid) not in (("18d1", "2d00"), ("18d1", "2d01"), ("12d1", "0001")):
                continue
            log(f"video: resetting the dongle's USB ({os.path.basename(dev)} {vid}:{pid})")
            with open(os.path.join(dev, "authorized"), "w") as f:
                f.write("0")
            time.sleep(2)
            with open(os.path.join(dev, "authorized"), "w") as f:
                f.write("1")
            return True
        except OSError as e:
            log(f"video: dongle USB reset failed: {e}")
    return False


def start_filler():
    ff = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi",
         "-i", f"color=c=0x101820:s={WIDTH}x{HEIGHT}:r={VIDEO_FPS}",
         "-vf", filler_filter(), *x264_args()],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    threading.Thread(target=drain_stderr, args=(ff.stderr, "ffmpeg(filler)", lambda l: True), daemon=True).start()
    return Pipeline("filler", [ff])


class VideoMux:
    """Runs the AA and filler pipelines and forwards one of them to the attached dash session.
    Switches only on an IDR, so the dash decoder never gets a P-frame from the wrong stream.
    Can outlive dash sessions (AA_ALWAYS): attach() / detach() a session as the dash comes and goes."""

    def __init__(self, session=None):
        self.session = session
        self.rtp = RtpPacketizer(lambda pkt: self.session and self.session.send_rtp(pkt))
        self.lock = threading.Lock()
        self.current = None
        self.last_aa_frame = 0.0
        self.last_pts = 0
        self.t0 = time.monotonic()
        self.stopping = threading.Event()
        self.pipes = {}
        self.aa_frames = 0
        self.sent_frames = 0

    def start(self):
        self.pipes["filler"] = start_filler()
        threading.Thread(target=self._read, args=("filler",), daemon=True).start()
        threading.Thread(target=self._aa_loop, daemon=True).start()

    def stop(self):
        self.stopping.set()
        for p in list(self.pipes.values()):
            p.stop()

    def attach(self, session):
        """Start feeding a new dash session, from the next keyframe of either source."""
        with self.lock:
            self.session = session
            self.current = None
            self.sent_frames = 0

    def detach(self):
        with self.lock:
            self.session = None
            self.current = None

    def send_input(self, line):
        """Input command for the running Android Auto session (see aa-headless usage)."""
        aa = self.pipes.get("aa")
        return bool(aa) and aa.send_input(line)

    def find_recentre(self):
        aa = self.pipes.get("aa")
        return aa.probe.find_recentre() if aa and hasattr(aa, "probe") else None

    def _aa_loop(self):
        failed_runs = 0
        last_reset = -AA_RESET_GAP_S
        while not self.stopping.is_set():
            log("video: starting aa-headless (waiting for the phone via the dongle)")
            started, frames = time.monotonic(), self.aa_frames
            self.pipes["aa"] = start_aa()
            self._read("aa")
            self.pipes["aa"].stop()
            # Several quick sessions in a row with no video: the dongle is probably stuck.
            if self.aa_frames == frames and time.monotonic() - started < AA_STUCK_RUN_S:
                failed_runs += 1
            else:
                failed_runs = 0
            if (failed_runs >= AA_STUCK_RUNS and time.monotonic() - last_reset >= AA_RESET_GAP_S
                    and not self.stopping.is_set()):
                failed_runs = 0
                last_reset = time.monotonic()
                if reset_dongle():
                    self.stopping.wait(5)
                    continue
            if not self.stopping.is_set():
                log("video: Android Auto session ended; restarting in 2 s")
                self.stopping.wait(2)

    def _read(self, name):
        """Reads one pipeline's encoder output. ffmpeg writes each frame in one go; a short
        idle gap after data marks the end of a frame."""
        pipe = self.pipes[name]
        fd = pipe.stdout.fileno()
        nals = NalProcessor()
        buf = b""
        while not self.stopping.is_set():
            ready, _, _ = select.select([fd], [], [], 0.03 if buf else 1.0)
            if ready:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                buf += chunk
                continue
            if buf:
                self._on_frame(name, nals.frame_units(buf))
                buf = b""
            elif not pipe.alive():
                break

    def _on_frame(self, name, units):
        if not units:
            return
        is_idr = any(idr for _, idr in units)
        now = time.monotonic()
        with self.lock:
            if name == "aa":
                self.aa_frames += 1
                self.last_aa_frame = now
                if self.aa_frames == 1:
                    log("video: first Android Auto frame from the encoder")
            if self.session is None:
                return
            if name != self.current:
                switch = is_idr and (name == "aa" or now - self.last_aa_frame > FILLER_AFTER_S)
                if not switch:
                    return
                log(f"video: dash now showing {name}")
                self.current = name
            if not self.session.streaming:
                return
            pts = max(int((now - self.t0) * 1000), self.last_pts + 1)
            self.last_pts = pts
            for unit, _ in units:
                self.rtp.packetize(unit, pts)
            self.sent_frames += 1


# ── Joystick → Android Auto ───────────────────────────────────────────────────

def _codes(name, default):
    return {int(c, 16) for c in os.environ.get(name, default).replace(",", " ").split()}


# Dash joystick codes in map mode (Wayfindr ride logs): RIGHT 0x13, LEFT 0x14, DOWN 0x15,
# UP probably 0x12, centre click 0x18 (the dash also opens its own media controls on it).
JOY_ZOOM_IN = _codes("JOY_ZOOM_IN", "0x13")
JOY_ZOOM_OUT = _codes("JOY_ZOOM_OUT", "0x14")
# 0x18 (centre click) is left to the dash: it switches the dash into its own music controls,
# where left/right arrive as 0x0A/0x09.
JOY_RECENTRE = _codes("JOY_RECENTRE", "0x15")
JOY_MEDIA_NEXT = _codes("JOY_MEDIA_NEXT", "0x09")
JOY_MEDIA_PREV = _codes("JOY_MEDIA_PREV", "0x0A")
# Centre click while the dash shows its music controls (bike log 2026-10-31 23:54).
JOY_MEDIA_PLAY_PAUSE = _codes("JOY_MEDIA_PLAY_PAUSE", "0x05")
# What each action sends to aa-headless; {x} {y} = centre of the map in AA's frame. Maps
# ignores gestures on its map, but taps on its own + / - buttons work; they show only in a
# big enough layout (VIDEO_MODE=fill, AA_DPI 110), where they sit at these AA frame points.
# Re-centre "auto" taps Maps' Re-centre button where ScreenProbe sees it, else presses AA's
# NAVIGATION key (brings Maps back if another app is up). Never a fixed tap: Re-centre moves
# between browse and route mode, and during a route the end-navigation X sits where it shows
# in browse mode; with no button showing, the spot is the map or the position marker.
AA_CMD_ZOOM_IN = os.environ.get("AA_CMD_ZOOM_IN", "tap 204 313" if LEFT_SEAT else "tap 596 313")
AA_CMD_ZOOM_OUT = os.environ.get("AA_CMD_ZOOM_OUT", "tap 204 357" if LEFT_SEAT else "tap 596 357")
AA_CMD_RECENTRE = os.environ.get("AA_CMD_RECENTRE", "auto")
# The stick can send a ghost second direction (RIGHT then DOWN); ignore a different
# direction this soon after the last press, as Wayfindr's JoystickDebounce does.
JOY_DEBOUNCE_MS = int(os.environ.get("JOY_DEBOUNCE_MS", "300"))


class Joystick:
    def __init__(self, video):
        self.video = video
        w, h, x, y = aa_crop()
        self.centre = {"x": x + w // 2, "y": y + h // 2}
        self.last_code = None
        self.last_ms = -10_000

    def on_button(self, code):
        now = time.monotonic() * 1000
        if code != self.last_code and now - self.last_ms < JOY_DEBOUNCE_MS:
            log(f"joystick: 0x{code:02X} ignored ({now - self.last_ms:.0f} ms after 0x{self.last_code:02X})")
            return
        self.last_code, self.last_ms = code, now
        # Android Auto media keys (advertised by aa-headless): 87 next, 88 previous, 85 play/pause.
        for codes, action, template in ((JOY_ZOOM_IN, "zoom in", AA_CMD_ZOOM_IN),
                                        (JOY_ZOOM_OUT, "zoom out", AA_CMD_ZOOM_OUT),
                                        (JOY_RECENTRE, "re-centre", AA_CMD_RECENTRE),
                                        (JOY_MEDIA_NEXT, "next track", "key 87"),
                                        (JOY_MEDIA_PREV, "previous track", "key 88"),
                                        (JOY_MEDIA_PLAY_PAUSE, "play/pause", "key 85")):
            if code in codes:
                if template == "auto":
                    spot = self.video.find_recentre()
                    template = f"tap {spot[0]} {spot[1]}" if spot else "key 65538"
                cmd = template.format(**self.centre)
                sent = self.video.send_input(cmd)
                log(f"joystick: 0x{code:02X} -> {action} ({cmd})" + ("" if sent else ", AA not running"))
                return


# ── K1G session (DashSession.kt) ──────────────────────────────────────────────

class SessionFailed(Exception):
    pass


class DashSession:
    def __init__(self, ssid):
        self.ssid = ssid
        self.auth = k1g.DashAuth(ssid)
        self.seq = 0
        self.seq_lock = threading.Lock()
        self.authenticated = threading.Event()
        self.failed = threading.Event()
        self.fail_reason = ""
        self.streaming = False
        self.last_rx = 0.0
        self.first_ack = False
        self.acked_frames = 0   # 09 06 / 09 04 acks: frames the dash has decoded
        self.auth_retries = 0
        self.closed = threading.Event()
        self.on_button = None   # called with each joystick/button code from the dash

        def sock(port=None, broadcast=False):
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, IFACE.encode())
            if broadcast:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            if port is not None:
                s.bind(("", port))
            return s

        # RX must be open before the first TX (catches the early pubkey; avoids ICMP unreachable).
        self.rx = sock(RX_PORT)
        self.rx.settimeout(0.5)
        self.tx = sock(CTRL_PORT, broadcast=True)
        self.rtp = sock()

    def send(self, pkt):
        with self.seq_lock:
            pkt = k1g.patch_seq(pkt, self.seq)
            self.seq = (self.seq + 1) & 0xFF
        try:
            self.tx.sendto(pkt, (BROADCAST, CTRL_PORT))
        except OSError as e:
            self.fail(f"control send failed: {e}")

    def send_rtp(self, pkt):
        try:
            self.rtp.sendto(pkt, (DASH_IP, RTP_PORT))
        except OSError:
            pass

    def fail(self, reason):
        if not self.failed.is_set():
            self.fail_reason = reason
            self.failed.set()

    def close(self):
        self.closed.set()
        self.streaming = False
        for pkt in (k1g.projection_stop(), k1g.projection_off()):
            try:
                self.tx.sendto(k1g.patch_seq(pkt, self.seq), (BROADCAST, CTRL_PORT))
            except OSError:
                pass
        for s in (self.rx, self.tx, self.rtp):
            s.close()

    def _every(self, period, fn):
        def loop():
            while not self.closed.is_set() and not self.failed.is_set():
                fn()
                self.closed.wait(period)
        threading.Thread(target=loop, daemon=True).start()

    def _rx_loop(self):
        while not self.closed.is_set():
            try:
                data = self.rx.recv(65535)
            except socket.timeout:
                continue
            except OSError as e:
                if not self.closed.is_set():
                    self.fail(f"receive failed: {e}")
                return
            self.last_rx = time.monotonic()
            for t, s, v in k1g.parse_incoming(data):
                self._dispatch(t, s, v)

    def _dispatch(self, t, s, v):
        if t == 0x07:
            ev = self.auth.ingest(s, v)
            if ev and ev[0] == "send":
                log("dash: got RSA public key, sending session key")
                self.send(ev[1])
            elif ev and ev[0] == "confirmed":
                self.authenticated.set()
            elif ev and ev[0] == "rejected":
                self.auth_retries += 1
                log(f"dash: auth rejected (retry {self.auth_retries}); is the SSID right? {self.ssid!r}")
                self.auth.reset()
                if self.auth_retries <= 5:
                    self.send(k1g.auth_request())
        elif t == 0x09 and s == 0x06 and v[:1] == b"\x55":
            if not self.first_ack:
                self.first_ack = True
                log("dash: decoded the first keyframe (09 06 55), video accepted")
            self.acked_frames += 1
            self.send(k1g.frame_decoded_idr())
        elif t == 0x09 and s == 0x04 and v[:1] == b"\x55":
            self.acked_frames += 1
            self.send(k1g.frame_decoded_p())
        elif t == 0x09 and s == 0x00 and v:
            code = v[-1]
            log(f"dash: button event 0x{code:02X}")
            self.send(k1g.button_ack(code))
            if self.on_button:
                self.on_button(code)
        elif t not in (0x0C, 0x0F) and (t, s) != (0x01, 0x01):   # 01 01 = the dash's 1 Hz counter
            log(f"dash: event {t:02X} {s:02X} {v.hex(' ')}")

    def _heartbeat(self):
        self._hb_count = getattr(self, "_hb_count", 0) + 1
        self.send(k1g.heartbeat())
        if self._hb_count % 30 == 1:
            self.send(k1g.time_sync())
        if self.streaming and self.first_ack and time.monotonic() - self.last_rx > RX_WATCHDOG_S:
            self.fail(f"dash silent for {RX_WATCHDOG_S}+ s")

    def run(self):
        """Handshake and nav-mode entry; returns once streaming may start."""
        threading.Thread(target=self._rx_loop, daemon=True).start()
        self._every(1.0, self._heartbeat)
        log(f"dash: connecting to {self.ssid}")
        for pkt in k1g.initial_burst(HOSTNAME):
            self.send(pkt)
            time.sleep(0.02)
        if not self.authenticated.wait(AUTH_TIMEOUT_S):
            raise SessionFailed(f"no auth confirmation (07 01 01) within {AUTH_TIMEOUT_S} s")
        log("dash: authenticated")
        self.send(k1g.nav_context()); time.sleep(0.04)
        self.send(k1g.empty_lists()); time.sleep(0.04)
        for i in range(4):
            self.send(route_card(False))
            time.sleep(0.1 if i < 1 else 0.5)
        self.send(k1g.projection_frame()); time.sleep(0.06)
        self.send(k1g.nav_placeholder()); time.sleep(0.01)
        self.send(k1g.nav_start()); time.sleep(0.04)
        self.send(route_card(True))
        log("dash: nav mode entered, streaming")
        self.streaming = True
        self._every(0.25, lambda: self.send(k1g.projection_frame()))
        self._every(1.0, lambda: self.send(route_card(True)))
        self._every(1.0, self._music)

    def _music(self):
        """Now-playing to the dash's music widget, ~1 Hz like the original app (the dash drops
        a card it stops hearing about). 05 17 must be on or the dash never shows the song."""
        title, artist, album, state = now_playing.snapshot()
        active = bool(title) and state != "none"
        if active:
            self.send(k1g.music_active(True))
            self.send(k1g.music_playing(state != "pause"))
            self.send(k1g.now_playing(title, album, artist))
        elif getattr(self, "_music_on", False):
            self.send(k1g.music_active(False))
        if active != getattr(self, "_music_on", False):
            log(f"dash: music widget {'on: ' + title if active else 'off'}")
        self._music_on = active


def run_once(ssid, video=None):
    """One dash session. With a long-running `video` (AA_ALWAYS) the session only attaches to
    it; otherwise the video pipeline starts and stops with the session."""
    session = DashSession(ssid)
    own_video = video is None
    if own_video:
        video = VideoMux(session)
        video.start()   # encoders warm up during the handshake
    else:
        video.attach(session)
    session.on_button = Joystick(video).on_button
    try:
        session.run()
        last_report = time.monotonic()
        backlog = []   # frames sent but not yet acked, sampled each second: the dash's queue
        while not session.failed.wait(1.0):
            if dash_ssid() != ssid:
                raise SessionFailed("left the dash Wi-Fi")
            if session.first_ack:
                backlog.append(video.sent_frames - session.acked_frames)
            if time.monotonic() - last_report >= 30:
                last_report = time.monotonic()
                lag = (f"; dash backlog avg {sum(backlog) / len(backlog):.1f} max {max(backlog)} frames"
                       if backlog else "")
                backlog = []
                log(f"stats: {video.sent_frames} frames sent to the dash, {session.acked_frames} acked, "
                    f"showing {video.current}, {video.aa_frames} AA frames encoded{lag}")
        raise SessionFailed(session.fail_reason)
    finally:
        if own_video:
            video.stop()
        else:
            video.detach()
        session.close()


def main():
    for tool in ("ffmpeg", "nmcli"):
        if not shutil.which(tool):
            log(f"missing {tool}")
            sys.exit(1)
    log(f"dash bridge: mode {VIDEO_MODE}, crop {'x'.join(map(str, aa_crop()[:2]))} of AA's 800x480, "
        f"{VIDEO_FPS} fps, {VIDEO_BITRATE}, opens {AA_LAUNCH}, aa-headless {AA_BIN}")
    video = None
    if AA_ALWAYS:
        # Keep Android Auto connected whenever the dongle is present, dash or not. Otherwise
        # the dongle restarts every ~14 s with nothing answering it and the phone keeps
        # opening and closing Android Auto.
        log("Android Auto runs whenever the dongle is present (AA_ALWAYS=1)")
        video = VideoMux()
        video.start()
    waiting_logged = False
    while True:
        ssid = dash_ssid()
        if not ssid:
            if not waiting_logged:
                log("waiting for the dash Wi-Fi")
                waiting_logged = True
            time.sleep(2)
            continue
        waiting_logged = False
        try:
            run_once(ssid, video)
        except SessionFailed as e:
            log(f"session ended: {e}")
        except OSError as e:
            log(f"session error: {e}")
        time.sleep(3)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
