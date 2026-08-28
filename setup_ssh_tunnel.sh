#!/usr/bin/env bash
set -euo pipefail

sudo cp /tmp/opencode/sshd-loopback.conf /etc/ssh/sshd_config.d/port.conf
sudo systemctl restart ssh
echo "1. sshd is now loopback-only (127.0.0.1:2222)"

sudo tailscale serve --bg --tcp 2222 tcp://127.0.0.1:2222
echo "2. Tailscale TCP proxy added: ssh ${USER}@<tailscale-address> -p 2222"

echo
echo "Done. Verify with: ss -tlnp | grep 2222"
