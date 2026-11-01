#!/bin/sh
# Installs the Wayfindr dash bridge on the Pi. Run from the folder holding these files:
#   sudo sh install.sh [path to the aa-headless folder]
# Re-running it updates the scripts and keeps an edited /etc/default/wayfindr-pi.
set -e
cd "$(dirname "$0")"
HU_SRC="${1:-$HOME/aa/hu}"

install -d /opt/wayfindr /opt/wayfindr/hu
install -m 0755 dash_wifi.py dash_bridge.py /opt/wayfindr/
install -m 0644 k1g.py selftest.py /opt/wayfindr/
if [ -x "$HU_SRC/aa-headless" ]; then
    cp -a "$HU_SRC"/aa-headless "$HU_SRC"/libaasdk* /opt/wayfindr/hu/
fi
[ -f /etc/default/wayfindr-pi ] || install -m 0644 wayfindr-pi.default /etc/default/wayfindr-pi
install -m 0644 wayfindr-dash-wifi.service wayfindr-dash-bridge.service /etc/systemd/system/

# gvfs's MTP monitor grabs the AA dongle (it calls its interface "MTP"). Undo with:
#   sudo systemctl --global unmask gvfs-mtp-volume-monitor.service
systemctl --global mask gvfs-mtp-volume-monitor.service >/dev/null 2>&1 || true

systemctl daemon-reload
systemctl enable wayfindr-dash-wifi.service wayfindr-dash-bridge.service
systemctl restart wayfindr-dash-wifi.service wayfindr-dash-bridge.service
echo "installed; logs: journalctl -u wayfindr-dash-wifi -u wayfindr-dash-bridge -f"
