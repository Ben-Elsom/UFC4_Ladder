# UFC4_Ladder

A bot account sits in the PlayStation group chat. Every hour a GitHub Action reads new messages,
logs results, replies with new ratings, and rebuilds the website in `docs/`.

- Two separate ladders: MMA and Stand & Bang (Glicko-2 ratings, belts, head-to-head).
- Players post results in plain words (`beat sam 2-1 mma`); names can be partial or misspelt.
- Best-of-threes are posted once with the score; every fight in the set counts.
- Disputes, unreadable posts and unanswered questions show on the site under **Needs review**.

## Files
- `ladder/parser.py` turns a message into a result. `ladder/names.py` guesses who was meant.
- `ladder/engine.py` holds the fight log, duplicates, disputes, ratings, belts and chat commands.
- `ladder/run.py` is one bot run. `ladder/admin.py` is for fixes. `ladder/template.html` is the website.
- `data/state.json` is the whole history (created on the first run). `config.json` holds settings.

## Settings (`config.json`)
- `group_name` or `group_id`: which chat to watch (not needed if the bot is only in one group chat).
- `admin_psn`: the admin's PSN name (gets the login-expiry message).
- `site_url`: the website link, shown in `!help`.
- `dry_run`: `true` reads the chat and builds the site but posts nothing.

## Secrets
- `PSN_NPSSO` (GitHub repository secret): the bot account's login code. Lasts about 2 months.

## Testing
    .venv/bin/python -m pytest -q
    .venv/bin/python -m ladder.run --fake chat.json --state /tmp/state.json --site /tmp/site

## Admin fixes (until the admin page exists)
On the server (`ssh ladder`, in `~/UFC4_Ladder`): `.venv/bin/python -m ladder.admin list | void N | restore N | edit N --winner x --loser y --score 2-1 --mode snb | swap N | resolve N`.
The running bot picks the change up straight away. A private admin web page is planned but parked; see `ADMIN_PAGE_PLAN.md`.

