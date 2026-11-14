# Admin page: parked plan

Status: **parked** (2026-11-14). The admin (BJkid2000) asked for it, then asked to hold off.
Nothing below has been built yet. Until it is, fixes are made by asking Claude, who runs
`python -m ladder.admin ...` on the server (see README).

## What the admin asked for

A private section of the website, for the admin only, where he can:
- see every confirmed result (newest first, with search)
- delete a result, and undo a deletion
- change who the winner or loser is (pick from the group members)
- change the score numbers and the mode
- (sensible extra) clear items from Needs review

Edits should recalculate ratings straight away. The public site updates within a minute.
The bot should **not** announce admin edits in the chat.

## Why it can't go on the current website

The public site is GitHub Pages, which only serves fixed pages and can't save anything.
The data lives on the Oracle server (`ssh ladder`, `/home/ubuntu/UFC4_Ladder/data/state.json`),
and `ladder/serve.py` owns it. So the admin page has to run on that server.

## Agreed design (what was proposed to the admin)

- **Same process as the bot.** Run a small web app (Flask + waitress, both free) in a thread inside
  `ladder/serve.py`. Share the in-memory `state` with the chat loop behind a `threading.Lock`.
  Don't run it as a separate process writing `state.json`: the bot loop saves its in-memory
  copy and would overwrite edits.
- **Edits reuse the existing logic** in `ladder/admin.py` (void / restore / edit winner, loser,
  score, mode / resolve review item). Move that logic into functions both the CLI and the web page call.
  After an edit, mark the state changed so serve.py rebuilds the site and pushes, as it does now.
- **HTTPS is required** because a password crosses the internet. Put Caddy (free) in front of the
  app; it gets a free Let's Encrypt certificate automatically.
- **Address: the admin hasn't chosen yet.** Ask him:
  - `https://161-33-93-95.sslip.io/admin`: works immediately. Ugly, and it breaks if the server's
    public IP changes (it's an *ephemeral* IP, 161.33.93.95, kept while the instance isn't terminated).
  - `https://admin.benelsom.com`: nicer and permanent. Needs one DNS A record → 161.33.93.95 wherever
    benelsom.com's DNS is managed. Ask him where that is; Claude can add it in the browser pane if he's signed in.
- **Login (only him):**
  - **Password:** he sets it himself in a terminal prompt over `ssh -t ladder`, the same pattern as
    `deploy/set_login_code.sh`. Store only a hash (e.g. scrypt) in `/home/ubuntu/ladder_admin.env`
    (chmod 600). Claude must never see or type the password.
  - **Lockout:** lock out after repeated wrong attempts.
  - **Sessions:** signed session cookie lasting 30 days. Secure, HttpOnly, SameSite=Strict, plus CSRF protection on edits.
- **Firewall:** open TCP 80 and 443 in two places:
  1. **Oracle Cloud:** add ingress rules for TCP 80 and 443 from 0.0.0.0/0 to the subnet's security
     list (VCN `vcn-20261114-1839`, subnet `subnet-20261114-1839`, Melbourne region).
  2. **The server's own iptables:** `INPUT` currently rejects everything except 22. Insert ACCEPT rules
     for 80 and 443 *before* the final REJECT, and persist them (e.g. `netfilter-persistent save`).
- **Cost:** everything is free.

## Pages to build

1. `/admin/login`: password only.
2. `/admin`: table of results (id, date, mode, winner, loser, score, status). Edit and delete buttons per row.
   Search box. Show deleted results greyed out with an Undo button.
3. Edit form: winner/loser dropdowns (current group members plus anyone with past fights), score inputs,
   mode (MMA / Stand & Bang), Save. Validate that winner and loser differ and the score isn't 0-0.
4. Needs review list with a Dismiss button (same as `ladder.admin resolve`).

Match the public site's look (`ladder/template.html`: Barlow Condensed headings, same colour tokens).
Make it work well on a phone.

## Before going live

- Write tests for the edit functions. Load the page locally against a copy of state.json.
- Check the page refuses everything without a login, and that login stops after repeated wrong passwords.
- Update the README and the project memory once it's live.
