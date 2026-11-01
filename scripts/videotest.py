#!/usr/bin/env python3
"""Runs the bridge's video path (aa-headless + filler + mux + RTP) without a dash, captures
the RTP it would send, and rebuilds the H.264 the dash would decode.
usage: sudo python3 videotest.py [seconds] [out.h264] [joystick script]
joystick script: "at:code,at:code" presses dash joystick codes (hex) through the bridge's
Joystick at those seconds, e.g. "30:13,33:13,36:14,42:18"."""

import sys
import time

import dash_bridge as b


class CaptureSession:
    streaming = True

    def __init__(self):
        self.packets = []

    def send_rtp(self, pkt):
        self.packets.append(pkt)


def depacketize(packets):
    """RTP -> Annex-B, reversing RtpPacketizer (single NAL or FU-A)."""
    out = bytearray()
    for p in packets:
        payload = p[12:]
        if payload[0] & 0x1F == 28:
            fu = payload[1]
            if fu & 0x80:
                out += b"\0\0\0\1" + bytes([(payload[0] & 0xE0) | (fu & 0x1F)])
            out += payload[2:]
        else:
            out += b"\0\0\0\1" + payload
    return bytes(out)


seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 45
out_path = sys.argv[2] if len(sys.argv) > 2 else "/tmp/dash-rtp.h264"
presses = sorted((float(at), int(code, 16)) for at, code in
                 (p.split(":") for p in sys.argv[3].split(","))) if len(sys.argv) > 3 else []
session = CaptureSession()
mux = b.VideoMux(session)
mux.start()
joystick = b.Joystick(mux)
t0 = time.monotonic()
last_print = 0
try:
    while time.monotonic() - t0 < seconds:
        time.sleep(0.2)
        now = time.monotonic() - t0
        while presses and presses[0][0] <= now:
            print(f"{now:4.1f} s: joystick 0x{presses[0][1]:02X}, frame {mux.sent_frames}", flush=True)
            joystick.on_button(presses.pop(0)[1])
        if now - last_print >= 5:
            last_print = now
            print(f"{now:4.0f} s: {mux.sent_frames} frames sent, showing {mux.current}, "
                  f"{mux.aa_frames} AA frames, {len(session.packets)} RTP packets", flush=True)
finally:
    mux.stop()

data = depacketize(session.packets)
open(out_path, "wb").write(data)
sizes = [len(p) for p in session.packets]
seqs = [int.from_bytes(p[2:4], "big") for p in session.packets]
gaps = sum(1 for a, c in zip(seqs, seqs[1:]) if (c - a) & 0xFFFF != 1)
print(f"wrote {out_path}: {len(data)} bytes from {len(sizes)} packets (max {max(sizes, default=0)} B, "
      f"seq gaps {gaps}); {len(data) / max(seconds, 1) / 1024:.1f} KB/s")
