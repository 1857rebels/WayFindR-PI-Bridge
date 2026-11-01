#!/bin/sh
# Runs Android Auto (Maps) at several screen densities and saves screenshots of each, to see
# which layouts show Maps' +/- zoom buttons. Run as root:
#   sudo sh dpisweep.sh "240x180" 140 120 100 90 80
# Output: /tmp/dpisweep/<margin>-<dpi>-{a,b}.png: a = Maps settled, b = 2 s after a tap on the
# map (the buttons may only show after a touch). The dash bridge is stopped while this runs
# and always restarted after, even if interrupted.
systemctl stop wayfindr-dash-bridge
trap 'systemctl start wayfindr-dash-bridge' EXIT
trap 'exit 1' INT TERM HUP
OUT=/tmp/dpisweep
AA=${AA_BIN:-/opt/wayfindr/hu/aa-headless}
MARGIN=$1; shift
rm -rf $OUT && mkdir -p $OUT
pkill -x gvfsd-mtp 2>/dev/null

# Cut off mid-session, the dongle can stay stuck until re-plugged; this re-plugs it.
reset_dongle() {
    for d in /sys/bus/usb/devices/*; do
        case "$(cat $d/idVendor 2>/dev/null):$(cat $d/idProduct 2>/dev/null)" in
            18d1:2d00|18d1:2d01|12d1:0001) echo 0 > $d/authorized; sleep 2; echo 1 > $d/authorized; sleep 6 ;;
        esac
    done
}

# Tap point in frame coordinates: the open map below the turn card (dome layout: map at
# x 120-610, y 90-390). Override with TAP="x y".
TAP=${TAP:-365 300}

for dpi in "$@"; do
    for try in 1 2 3; do
        T=$OUT/run && rm -rf $T && mkdir -p $T
        {
            # Maps opens 3 s after the first frame; give it time to settle.
            n=0
            while [ "$(ls $T | grep -c png)" -lt 10 ] && [ $n -lt 25 ]; do sleep 1; n=$((n + 1)); done
            [ "$(ls $T | grep -c png)" -lt 10 ] && exit 0
            sleep 4
            echo "$(ls $T | grep -c png)" > $T/mark_a
            echo "tap $TAP"
            sleep 2
            echo "$(ls $T | grep -c png)" > $T/mark_b
            sleep 2
        } | timeout -s INT 60 $AA - $MARGIN $dpi maps 2>$T/aa.log | \
            ffmpeg -v error -probesize 32768 -analyzeduration 0 -use_wallclock_as_timestamps 1 \
                -f h264 -i pipe:0 -vf fps=1 $T/%03d.png
        if [ -s $T/mark_b ]; then
            cp $T/$(printf %03d $(cat $T/mark_a)).png $OUT/$MARGIN-$dpi-a.png
            cp $T/$(printf %03d $(cat $T/mark_b)).png $OUT/$MARGIN-$dpi-b.png
            echo "dpi $dpi: ok"
            break
        fi
        echo "dpi $dpi: no video (try $try), resetting the dongle"
        reset_dongle
    done
    sleep 3
done
ls $OUT
