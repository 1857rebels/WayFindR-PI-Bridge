#!/usr/bin/env python3
"""Offline checks for k1g.py and the bridge's video path (no network, no dash needed).
usage: selftest.py [encoded.h264]"""

import sys

from cryptography.hazmat.primitives.asymmetric import padding, rsa

import dash_bridge as b
import k1g

H = k1g.hexb

# Packets that are literal hex in Wayfindr must survive the rebuild path unchanged.
assert k1g.build((0x05, 0x56, b"\x55")) == k1g.projection_frame()
assert k1g.build((0x06, 0x11, b"\x55")) == k1g.frame_decoded_idr()
assert k1g.build((0x06, 0x80, b"\x14")) == H("0016000200000000020100054B314720000680000114")
assert k1g.patch_seq(k1g.projection_frame(), 0x1FF)[16] == 0xFF

# hostnameAnnounce("WayfindR"): 16-byte header, 01 (seq), TLV 06 0B len 9, name, NUL.
ha = k1g.hostname_announce("WayfindR")
assert ha == H("001e0002 00000000 0201 0005 4b314720 01 060B 0009") + b"WayfindR\0", ha.hex()

# Route card: title replaced, length fixed up, projection flag and idle fields patched.
card = k1g.idle_route_card("Android Auto", True)
assert int.from_bytes(card[:2], "big") == len(card)
assert b"\x05\x01\x00\x0dAndroid Auto\x00" in card
assert card[card.rfind(H("06050001")) + 4] == 0x55
assert card[card.rfind(H("05020001")) + 4] == 0xFF
assert card[card.rfind(H("05090002")) + 4:][:2] == b"\0\0"
assert card[card.rfind(H("05080004")) + 4:][:4] == b"    "
assert k1g.route_card("x", False)[k1g.route_card("x", False).rfind(H("06050001")) + 4] == 0xAA
assert k1g.parse_incoming(H("0010 0002 00000000 0701 0001 01")) == [(0x07, 0x01, b"\x01")]

# Heartbeat fields land where DashCommands.heartbeat puts them.
hb = k1g.heartbeat(temp_c=30, heading_deg=370, battery_pct=80)
assert hb[hb.find(H("06100001")) + 4] == 70
assert hb[hb.find(H("06040001")) + 4] == 180
assert hb[hb.find(H("052D0002")) + 4:][:2] == (10).to_bytes(2, "big")

# Auth: the dash's key arrives in two TLVs; the reply decrypts to ssid || 32-byte key.
priv = rsa.generate_private_key(public_exponent=65537, key_size=1024)
pub = priv.public_key().public_numbers()
auth = k1g.DashAuth("RE_TEST_123")
assert auth.ingest(0x00, pub.n.to_bytes(128, "big")) is None
kind, pkt = auth.ingest(0x03, pub.e.to_bytes(3, "big"))
assert kind == "send" and len(pkt) == 0x95
plain = priv.decrypt(pkt[-128:], padding.PKCS1v15())
assert plain == b"RE_TEST_123" + auth.session_key
assert auth.ingest(0x01, b"\x01") == ("confirmed",)

# NAL split: 3- and 4-byte start codes, trailing zero before a 4-byte code.
assert b.split_nals(H("00000001 6742 000001 68ce 00 00000001 6588")) == [H("6742"), H("68ce00"), H("6588")]

# RTP: FU-A for a big NAL, marker only on its last packet.
pkts = []
rtp = b.RtpPacketizer(pkts.append)
rtp.packetize(bytes([0x65]) + bytes(3000), 1000)
assert len(pkts) == 3 and [p[1] >> 7 for p in pkts] == [0, 0, 1]
assert pkts[0][12] == 0x7C and pkts[0][13] == 0x85 and pkts[-1][13] == 0x45
assert all(len(p) <= 12 + 1380 for p in pkts)

if len(sys.argv) > 1:
    data = open(sys.argv[1], "rb").read()
    proc = b.NalProcessor()
    units = proc.frame_units(data)
    idrs = [u for u, idr in units if idr]
    print(f"{sys.argv[1]}: {len(units)} units, {len(idrs)} IDR bundles, "
          f"SPS {proc.sps[:4].hex(' ') if proc.sps else None}")
    assert idrs and idrs[0][:4] == H("67420029"), idrs[0][:8].hex()
    assert all(u[0] & 0x1F not in (6, 9) for u, _ in units)

print("selftest OK")
