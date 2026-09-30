#!/bin/sh
# Install the ZKTeco bridge as a system service.
#
# Deliberately a readable shell script rather than a .deb: a customer's
# sysadmin will read this before running it, which is the point.
set -eu

VERSION="${1:-$(cat VERSION 2>/dev/null || echo 1.0.0)}"
TARGET="/opt/zkbridge/${VERSION}"

[ "$(id -u)" -eq 0 ] || { echo "run this as root" >&2; exit 1; }

id -u zkbridge >/dev/null 2>&1 || \
    useradd --system --no-create-home --shell /usr/sbin/nologin zkbridge

mkdir -p "$TARGET"
cp -r ./zkbridge/. "$TARGET/"
ln -sfn "$TARGET" /opt/zkbridge/current

install -m 0644 zkbridge.service /etc/systemd/system/zkbridge.service
systemctl daemon-reload
systemctl enable --now zkbridge

sleep 3
PIN="$(cat /etc/zkbridge/ui_pin.txt 2>/dev/null || echo '(see the log)')"
cat <<EOF

ZKTeco bridge ${VERSION} installed and started.

  Open        http://127.0.0.1:8709/
  Access code ${PIN}

Enter the Odoo address and the bridge token from
Attendances -> Configuration -> LAN Bridges.

This machine needs OUTBOUND 443 to Odoo and OUTBOUND 4370 to each clock.
Nothing inbound. Do not open a firewall port for it.
EOF
