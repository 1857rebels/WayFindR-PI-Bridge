#!/bin/sh
# Interactive AA session: commands via /tmp/aa.in, a screenshot each second at /tmp/live/now.png.
systemctl stop wayfindr-dash-bridge
trap "systemctl start wayfindr-dash-bridge" EXIT
trap "exit 1" INT TERM HUP
rm -rf /tmp/live /tmp/aa.in; mkdir -p /tmp/live; mkfifo /tmp/aa.in
pkill -x gvfsd-mtp
for d in /sys/bus/usb/devices/*; do
    [ "$(cat $d/idVendor 2>/dev/null)" = 18d1 ] && { echo 0 > $d/authorized; sleep 2; echo 1 > $d/authorized; sleep 6; }
done
( exec 3<>/tmp/aa.in; timeout 600 cat <&3 ) | timeout -s INT 600 /opt/wayfindr/hu/aa-headless - 240x180 110 maps 2>/tmp/live/aa.log | \
  ffmpeg -v error -probesize 32768 -analyzeduration 0 -use_wallclock_as_timestamps 1 -f h264 -i pipe:0 -vf fps=1 -update 1 /tmp/live/now.png
