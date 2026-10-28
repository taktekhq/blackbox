#!/bin/sh
# Install blackbox as a root systemd service and start it. Run with sudo.
# PREFIX (default /usr/local) sets where the command goes.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
prefix=${PREFIX:-/usr/local}

install -D -m 0755 "$here/blackbox.py" "$prefix/bin/blackbox"
sed "s|/usr/local/bin/blackbox|$prefix/bin/blackbox|" "$here/blackbox.service" > /etc/systemd/system/blackbox.service

# Throttle reasons come from /dev/cpu/*/msr. Without it blackbox still records everything else.
if modprobe msr 2>/dev/null; then
    echo msr > /etc/modules-load.d/blackbox.conf
else
    echo "blackbox: no msr module, so throttle reasons will be missing" >&2
fi

systemctl daemon-reload
systemctl enable blackbox.service
systemctl restart blackbox.service
echo "blackbox: recording to /var/log/blackbox. Read it with: blackbox report"
