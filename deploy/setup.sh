#!/bin/bash
# One-time server setup (Ubuntu). Run as the ubuntu user from inside the cloned repo.
set -e
sudo apt-get update -q && sudo apt-get install -y -q python3-venv git
cd /home/ubuntu/UFC4_Ladder
python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
git config user.name "ladder-bot"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
touch /home/ubuntu/ladder.env && chmod 600 /home/ubuntu/ladder.env
sudo cp deploy/ladder.service /etc/systemd/system/ladder.service
sudo timedatectl set-timezone Australia/Melbourne || true
sudo systemctl daemon-reload
sudo systemctl enable ladder
echo "Setup done. Add the login code to /home/ubuntu/ladder.env, then: sudo systemctl restart ladder"
