#!/usr/bin/env bash
set -euo pipefail

sudo cp /tmp/opencode/sshd-loopback.conf /etc/ssh/sshd_config.d/port.conf
sudo systemctl restart ssh
echo "sshd is now loopback-only (127.0.0.1:2222)"
