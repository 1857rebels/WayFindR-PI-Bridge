#!/bin/sh
# Tries Android Auto input commands against Maps and saves one screenshot a second, to see
# which inputs zoom and recentre. Run as root:
#   sudo [DPI=110] [MARGIN=240x180] sh inputtest.sh "pinch in 365 240" "pinch out 365 240" ...
# Output: /tmp/inputtest/NNN.png and /tmp/inputtest/log (screenshot number per command).
# The dash bridge is stopped while this runs (it needs the dongle) and always restarted after,
# even if the test is interrupted.
systemctl stop wayfindr-dash-bridge
trap 'systemctl start wayfindr-dash-bridge' EXIT
trap 'exit 1' INT TERM HUP
OUT=/tmp/inputtest
AA=${AA_BIN:-/opt/wayfindr/hu/aa-headless}
rm -rf $OUT && mkdir -p $OUT
pkill -x gvfsd-mtp 2>/dev/null

for try in 1 2 3 4 5 6; do
    rm -f $OUT/*.png
    {
        # Wait for Maps: video running and the NAVIGATION key pressed 3 s after it starts.
        n=0
        while [ "$(ls $OUT | grep -c png)" -lt 8 ] && [ $n -lt 25 ]; do sleep 1; n=$((n + 1)); done
        [ "$(ls $OUT | grep -c png)" -lt 8 ] && exit 0
        sleep 3
        for cmd in "$@"; do
            echo "$(ls $OUT | grep -c png) $cmd" >> $OUT/log
            echo "$cmd"
            sleep 5
        done
        sleep 2
    } | timeout -s INT 120 $AA - ${MARGIN:-240x180} ${DPI:-140} maps 2>$OUT/aa.log | \
        ffmpeg -v error -probesize 32768 -analyzeduration 0 -use_wallclock_as_timestamps 1 \
            -f h264 -i pipe:0 -vf fps=1 $OUT/%03d.png
    [ -s $OUT/log ] && break
    sleep 4
done
cat $OUT/log
