#!/bin/bash
# Asks for the bot's NPSSO code (shown as you type), saves it on the server and restarts the bot.
echo "Paste the bot's NPSSO code, then press Enter:"
read -r CODE
CODE="$(printf '%s' "$CODE" | tr -d '[:space:]"')"
if [ ${#CODE} -ne 64 ]; then
  echo "That was ${#CODE} characters, but the code should be 64. Nothing saved. Run this again."
  exit 1
fi
printf 'PSN_NPSSO=%s\n' "$CODE" > /home/ubuntu/ladder.env
chmod 600 /home/ubuntu/ladder.env
sudo systemctl restart ladder
sleep 15
sudo journalctl -u ladder -n 5 --no-pager -o cat
echo "Saved. Tell Claude it's done."
