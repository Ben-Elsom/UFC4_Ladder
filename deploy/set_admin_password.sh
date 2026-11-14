#!/bin/bash
# Sets (or changes) the admin page password. Typing is hidden. Only a scrypt hash is stored,
# in /home/ubuntu/ladder_admin.env (readable only by the ubuntu user). Changing it logs out every device.
cd /home/ubuntu/UFC4_Ladder || exit 1
.venv/bin/python -m ladder.admin_web set-password || exit 1
sudo systemctl restart ladder
sleep 6
sudo journalctl -u ladder -n 20 --no-pager -o cat | grep -i "admin page"
echo "Done. Tell Claude it's done."
