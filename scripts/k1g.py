"""K1G dash protocol, ported from Wayfindr (app/src/main/java/com/example/wayfindr/dash):
protocol/K1GPacket.kt, protocol/DashCommands.kt and DashAuth.kt.

Outgoing packet: [len:2][segcount:2] 00000000 0201 0005 "K1G " [seq] then TLVs
(type, sub, len:2, value). Incoming packets carry their TLVs from offset 8.
"""

import datetime
import os

from cryptography.hazmat.primitives.asymmetric import padding, rsa

MAGIC = b"K1G "


def hexb(s):
    return bytes.fromhex(s.replace(" ", ""))


def _set_len(pkt):
    pkt[0:2] = len(pkt).to_bytes(2, "big")
    return bytes(pkt)


def build(*tlvs):
    """tlvs: (type, sub, value bytes). The seq byte is patched at send time."""
    pkt = bytearray(2) + (1 + len(tlvs)).to_bytes(2, "big") + bytes([0, 0, 0, 0, 2, 1, 0, 5]) + MAGIC + b"\0"
    for t, s, v in tlvs:
        pkt += bytes([t, s]) + len(v).to_bytes(2, "big") + v
    return _set_len(pkt)


def patch_seq(pkt, seq):
    out = bytearray(pkt)
    k = out.find(MAGIC)
    if 0 <= k and k + 4 < len(out):
        out[k + 4] = seq & 0xFF
    return _set_len(out)


def parse_incoming(data):
    """Dash -> app packet: list of (type, sub, value)."""
    tlvs = []
    if len(data) < 8:
        return tlvs
    count = int.from_bytes(data[2:4], "big")
    i = 8
    while len(tlvs) < count and i + 4 <= len(data):
        t, s, n = data[i], data[i + 1], int.from_bytes(data[i + 2:i + 4], "big")
        i += 4
        tlvs.append((t, s, data[i:i + n]))
        i += n
    return tlvs


# ── Auth ──────────────────────────────────────────────────────────────────────

def auth_request():
    return hexb("0016000200000000020100054b314720000804000101")


def auth_send_key(ciphertext):
    assert len(ciphertext) == 128, f"expected 128 B RSA ciphertext, got {len(ciphertext)}"
    return hexb("0095000200000000020100054B3147200008000080") + ciphertext


class DashAuth:
    """RSA-1024 + AES-256 handshake. The dash sends its modulus (07 00) and exponent (07 03),
    possibly in separate packets; we answer with RSA(ssid || random AES-256 key)."""

    def __init__(self, ssid):
        self.ssid = ssid
        self.reset()
        self.session_key = None

    def reset(self):
        self.modulus = None
        self.exponent = None
        self.key_sent = False

    def ingest(self, sub, value):
        """Returns ("send", packet), ("confirmed",), ("rejected",) or None."""
        if sub == 0x00:
            self.modulus = int.from_bytes(value, "big")
        elif sub == 0x03:
            self.exponent = int.from_bytes(value, "big")
        elif sub == 0x01:
            return ("confirmed",) if value[:1] == b"\x01" else ("rejected",)
        else:
            return None
        if not self.key_sent and self.modulus and self.exponent:
            self.key_sent = True
            self.session_key = os.urandom(32)
            key = rsa.RSAPublicNumbers(self.exponent, self.modulus).public_key()
            ct = key.encrypt(self.ssid.encode() + self.session_key, padding.PKCS1v15())
            return ("send", auth_send_key(ct))
        return None


# ── Session setup ─────────────────────────────────────────────────────────────

def time_sync():
    now = datetime.datetime.now()
    return build((0x06, 0x06, bytes([now.hour, now.minute, now.second])))


def hostname_announce(hostname):
    raw = hostname.encode()[:200]
    pkt = bytearray(hexb("0021000200000000020100054b314720")) + bytes([0x01, 0x06, 0x0B, 0x00, len(raw) + 1]) + raw + b"\0"
    return _set_len(pkt)


def initial_burst(hostname):
    return [
        auth_request(),
        hostname_announce(hostname),
        time_sync(),
        hexb("0016000200000000020100054b314720030557000155"),
        hexb("0016000200000000020100054b3147200405560001aa"),
        hexb("0016000200000000020100054b3147200506050001aa"),
        hexb("0016000200000000020100054b3147200605170001aa"),
        hexb("001d000200000000020100054b314720080a020008aa55000000000000"),
        hexb("0044000a00000000020100054b3147200906080001ff060300015506040001a2060f0001aa"
             "0601000101054c000113052d00020000051b0001190521000132054d000132"),
    ]


def nav_context():
    return hexb("0016000200000000020100054B31472000052E00011E")


def empty_lists():
    return hexb("002A000600000000020100054B31472000052F0001000530000100053100010005320001000533000100")


def nav_start():
    return hexb("0016000200000000020100054B31472000068000010B")


def nav_placeholder():
    return build((0x06, 0x0A, b"\0\0"))


def projection_frame():
    return hexb("0016000200000000020100054B314720000556000155")


def projection_stop():
    return hexb("0016000200000000020100054B3147200005560001AA")


def projection_off():
    return hexb("0016000200000000020100054B3147200006050001AA")


def frame_decoded_idr():
    return hexb("0016000200000000020100054B314720000611000155")


def frame_decoded_p():
    return hexb("0016000200000000020100054B314720000612000155")


def button_ack(code):
    return build((0x06, 0x80, bytes([code])))


# ── 1 Hz status heartbeat ─────────────────────────────────────────────────────

_HB = hexb("0049000b00000000020100054b3147200006080001050610000139060300015506040001a2060f0001aa"
           "0601000101054c000113052d00020000051b0001190521000132054d000132")


def heartbeat(temp_c=25, heading_deg=0, battery_pct=62, charging=False, signal=True, gps_on=True,
              media_volume=3, call_volume=9):
    pkt = bytearray(_HB)

    def put(t, s, v):
        at = pkt.find(bytes([t, s, 0, 1]))
        if at >= 0 and at + 4 < len(pkt):
            pkt[at + 4] = v & 0xFF

    put(0x06, 0x04, max(0, min(100, battery_pct)) + 100)
    put(0x06, 0x0F, 0x55 if charging else 0xAA)
    put(0x06, 0x01, 0x01 if signal else 0x00)
    put(0x06, 0x03, 0x55 if gps_on else 0xAA)
    put(0x05, 0x4C, 0x10 + max(0, min(10, media_volume)))
    put(0x05, 0x1B, 0x10 + max(0, min(10, call_volume)))
    put(0x06, 0x10, temp_c + 40)
    h = pkt.find(bytes([0x05, 0x2D, 0, 2]))
    deg = heading_deg % 360
    if h >= 0 and h + 5 < len(pkt):
        pkt[h + 4:h + 6] = deg.to_bytes(2, "big")
    return bytes(pkt)


# ── Route card ────────────────────────────────────────────────────────────────

_NAV_TEMPLATE = hexb(
    "007e001100000000020100054b31472025050100145461696c6c65206465204d617320647520477200"
    "050200013c050300013405050002000a05060001300507000130050800043033303305540001300509"
    "0002004f0546000110050a000155050c000104050b0006303031303030055500012006050001aa060d0001aa")
_seq_off = _NAV_TEMPLATE.find(MAGIC) + 4
_title_len = int.from_bytes(_NAV_TEMPLATE[_seq_off + 3:_seq_off + 5], "big")
_NAV_PREFIX = _NAV_TEMPLATE[:_seq_off]
_NAV_SUFFIX = _NAV_TEMPLATE[_seq_off + 5 + _title_len:]

NAV_NONE = 0xFF


def route_card(title, projection_on, maneuver=None, primary_unit=None, total_dist=None, total_unit=None,
               eta_hhmm=None, secondary=None, secondary_dist=None, secondary_unit=None):
    rt = title.encode()[:60] + b"\0"
    pkt = bytearray(_NAV_PREFIX) + b"\0" + bytes([0x05, 0x01]) + len(rt).to_bytes(2, "big") + rt + _NAV_SUFFIX

    def find_from_end(marker):
        return pkt.rfind(marker)

    def patch1(t, s, v):
        m = find_from_end(bytes([t, s, 0, 1]))
        if m >= 0 and m + 4 < len(pkt):
            pkt[m + 4] = v & 0xFF

    def patch2(t, s, v):
        m = find_from_end(bytes([t, s, 0, 2]))
        if m >= 0 and m + 5 < len(pkt):
            pkt[m + 4:m + 6] = (v & 0xFFFF).to_bytes(2, "big")

    patch1(0x06, 0x05, 0x55 if projection_on else 0xAA)
    if maneuver is not None:
        patch1(0x05, 0x02, maneuver)
    if primary_unit is not None:
        patch1(0x05, 0x06, primary_unit)
    patch2(0x05, 0x09, total_dist or 0)
    patch1(0x05, 0x03, NAV_NONE if secondary is None else secondary)
    patch2(0x05, 0x05, secondary_dist or 0)
    if secondary_unit is not None:
        patch1(0x05, 0x07, secondary_unit)
    if total_unit is not None:
        patch1(0x05, 0x46, total_unit)
    if eta_hhmm is not None and len(eta_hhmm) == 4:
        m = find_from_end(bytes([0x05, 0x08, 0, 4]))
        if m >= 0 and m + 8 < len(pkt):
            pkt[m + 4:m + 8] = eta_hhmm.encode()
    return _set_len(pkt)


def idle_route_card(title, projection_on):
    """Route card with no turn shown (Wayfindr's HIDE_TURN_WHEN_IDLE card)."""
    return route_card(title, projection_on, maneuver=NAV_NONE, primary_unit=0, total_dist=0, total_unit=0,
                      eta_hhmm="    ", secondary=NAV_NONE, secondary_dist=0, secondary_unit=0)


# ── Music widget (DashCommands.kt musicActive / musicPlaying / nowPlaying) ────

MEDIA_FIELD_MAX = 19   # the original app cuts each field to 19 chars


def _media_field(s):
    """One byte per char, like the original app: the dash font is single-byte."""
    return bytes(min(ord(c), 0xFF) for c in s[:MEDIA_FIELD_MAX])


def music_active(on):
    """05 17: the dash's music widget on (55) or off (AA); required before it shows a song."""
    return build((0x05, 0x17, bytes([0x55 if on else 0xAA])))


def music_playing(playing):
    """05 19: playing (55) or paused (AA)."""
    return build((0x05, 0x19, bytes([0x55 if playing else 0xAA])))


def now_playing(title, album, artist):
    """05 0D: title, album, artist, NUL-separated (the nav screen shows only the first)."""
    value = _media_field(title) + b"\0" + _media_field(album) + b"\0" + _media_field(artist)
    return build((0x05, 0x0D, value))
