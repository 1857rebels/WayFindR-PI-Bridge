#!/usr/bin/env python3
"""Measures how long the Pi's re-encode (the bridge's ffmpeg: decode AA, fps, crop, scale,
x264) holds a frame. Feeds a synthetic 30 fps 800x480 stream that switches from black to a
moving test pattern every few seconds, and times each switch from being written to ffmpeg to
the first large frame coming out. Runs alongside the bridge (same CPU load as on the bike).
    set -a; . /etc/default/wayfindr-pi; set +a; python3 /opt/wayfindr/latencytest.py
"""

import os
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dash_bridge as db

AA_FPS = 30
SEGMENT_S = 3      # black, pattern, black, pattern, ...
SEGMENTS = 8
BIG_FRAME = 1500   # bytes; black P-frames are far smaller, pattern frames far larger


def make_input(path):
    parts = []
    for i in range(SEGMENTS):
        src = "color=c=black:" if i % 2 == 0 else "testsrc2="
        parts += ["-f", "lavfi", "-i", f"{src}s=800x480:r={AA_FPS}:d={SEGMENT_S}"]
    concat = ("".join(f"[{i}:v]format=yuv420p,setsar=1[v{i}];" for i in range(SEGMENTS))
              + "".join(f"[v{i}]" for i in range(SEGMENTS)) + f"concat=n={SEGMENTS}:v=1:a=0")
    subprocess.run(["ffmpeg", "-v", "error", "-y", *parts, "-filter_complex", concat,
                    "-c:v", "libx264", "-preset", "ultrafast", "-profile:v", "baseline",
                    "-g", "600", "-x264-params", "aud=1", "-f", "h264", path], check=True)


def access_units(data):
    """Split on access unit delimiters (00 00 00 01 09)."""
    marks = []
    i = data.find(b"\0\0\0\1\x09")
    while i >= 0:
        marks.append(i)
        i = data.find(b"\0\0\0\1\x09", i + 1)
    return [data[a:b] for a, b in zip(marks, marks[1:] + [len(data)])]


def main():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "in.h264")
        make_input(path)
        with open(path, "rb") as f:
            aus = access_units(f.read())
    print(f"{len(aus)} input frames; filter: {db.aa_filter()}")

    # Variants to compare: LT_THREADS (decoder threads), LT_LOW_DELAY=1, LT_FILTER (replaces
    # the frame picker, e.g. fps=4).
    vf = db.aa_filter()
    if os.environ.get("LT_FILTER"):
        vf = os.environ["LT_FILTER"] + vf[vf.index(",crop="):]
    dec = ["-threads", os.environ.get("LT_THREADS", "2")]
    if os.environ.get("LT_LOW_DELAY") == "1":
        dec += ["-flags", "low_delay"]
    print(f"decoder {' '.join(dec)}; filter {vf.split(',')[0]}")
    ff = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-probesize", "32768", "-analyzeduration", "0",
         "-use_wallclock_as_timestamps", "1", "-f", "h264", *dec, "-i", "pipe:0",
         "-vf", vf, *db.x264_args()],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    switches = []   # time each pattern segment's first frame was written

    def feed():
        start = time.monotonic()
        for n, au in enumerate(aus):
            time.sleep(max(0, start + n / AA_FPS - time.monotonic()))
            if n % (AA_FPS * SEGMENT_S) == 0 and (n // (AA_FPS * SEGMENT_S)) % 2 == 1:
                switches.append(time.monotonic())
            ff.stdin.write(au)
            ff.stdin.flush()
        time.sleep(1)
        ff.stdin.close()

    threading.Thread(target=feed, daemon=True).start()

    # Output frames, as the bridge reads them: an idle gap after data ends a frame.
    frames = []
    fd = ff.stdout.fileno()
    buf = b""
    import select
    while True:
        ready, _, _ = select.select([fd], [], [], 0.03 if buf else 2.0)
        if ready:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            buf += chunk
            continue
        if buf:
            frames.append((time.monotonic(), len(buf)))
            buf = b""
        elif ff.poll() is not None:
            break

    delays = []
    for t in switches:
        first = next((ft for ft, size in frames if ft >= t and size >= BIG_FRAME), None)
        if first is not None:
            delays.append((first - t) * 1000)
    print(f"{len(frames)} output frames; switch -> first big output frame (ms): "
          + ", ".join(f"{d:.0f}" for d in delays))
    if delays:
        print(f"average {sum(delays) / len(delays):.0f} ms (includes up to {1000 // int(db.VIDEO_FPS)} ms "
              f"of waiting for the next {db.VIDEO_FPS} fps slot)")


if __name__ == "__main__":
    main()
