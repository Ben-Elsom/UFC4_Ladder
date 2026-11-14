"""One bot run: read new chat messages, reply, save, rebuild the website.

    python -m ladder.run                     # real PlayStation chat (needs PSN_NPSSO)
    python -m ladder.run --fake chat.json    # pretend chat, for testing
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import traceback
from pathlib import Path

from .engine import DAY, Ladder, new_state
from .site import build_site

ROOT = Path(__file__).resolve().parent.parent
LOGIN_LIFETIME_DAYS = 60     # an NPSSO code lasts about two months
LOGIN_WARN_DAYS = 7          # message the admin this long before it runs out
WEEKLY_POST_DAYS = 7

INTRO = (
    "👋 I'm the ladder bot. I keep the MMA and Stand & Bang rankings for this chat.\n"
    "After a fight, post the result like:  beat sam 2-1 mma  (or  lost to temp sb)\n"
    "I'll reply with everyone's new rating. Type !help for commands."
)


def load_config() -> dict:
    cfg = json.loads((ROOT / "config.json").read_text())
    for key in ("group_id", "group_name", "site_url", "admin_psn"):
        env = os.environ.get(key.upper())
        if env:
            cfg[key] = env
    if os.environ.get("DRY_RUN"):
        cfg["dry_run"] = os.environ["DRY_RUN"].lower() in ("1", "true", "yes")
    return cfg


def load_state(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else new_state()


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=1, ensure_ascii=False))


def run(chat, state: dict, cfg: dict, now: int, npsso: str = "") -> dict:
    meta = state["meta"]
    dry = cfg.get("dry_run", True)
    meta["dry_run"] = dry
    meta["admin_psn"] = cfg.get("admin_psn", "")
    ladder = Ladder(state, admin_name=cfg.get("admin_name") or cfg.get("admin_psn") or "the admin", site_url=cfg.get("site_url", ""))

    def say(text: str) -> None:
        state["log"].append({"ts": now, "text": text, "sent": not dry})
        state["log"] = state["log"][-200:]
        if not dry:
            chat.send(text)

    acct, online = chat.identity()
    state["bot"] = {"account_id": acct, "online_id": online}
    gid, gname = chat.group_label()
    meta["group"] = gname
    ladder.sync_members(chat.members())

    messages = chat.messages(limit=100)
    if not meta.get("started"):
        # First run: don't treat old chat history as results.
        for m in messages:
            ladder.handle(dict(m, body=""))
        meta["started"] = now
        meta["last_weekly"] = now
    else:
        unseen = [m for m in messages if m["uid"] not in set(state["seen"])]
        if len(messages) >= 100 and len(unseen) == len(messages):
            meta["gap_warning"] = now  # more than 100 messages since last run; some may be missed
        ladder.expire_questions(now)
        for m in unseen:
            for reply in ladder.handle(m):
                say(reply)

    if not dry and not meta.get("intro_sent"):
        say(INTRO)  # first time the bot is allowed to talk
        meta["intro_sent"] = now

    if now - meta.get("last_weekly", now) >= WEEKLY_POST_DAYS * DAY:
        lines = ladder._command({"sender_id": acct, "ts": now}, ["board"])
        if lines:
            say("📅 Weekly standings\n\n" + lines[0])
        meta["last_weekly"] = now

    # Login lifetime: remember when the current code was first used.
    if npsso:
        h = hashlib.sha256(npsso.encode()).hexdigest()[:16]
        if meta.get("login_hash") != h:
            meta.update(login_hash=h, login_since=now, login_warned=False)
        days_left = LOGIN_LIFETIME_DAYS - (now - meta["login_since"]) / DAY
        meta["login_days_left"] = round(days_left, 1)
        if days_left <= LOGIN_WARN_DAYS and not meta.get("login_warned"):
            admin = next((a for a, p in state["players"].items() if p["name"].lower() == (cfg.get("admin_psn") or "").lower()), None)
            if admin and not dry:
                chat.direct_message(admin, f"🔑 Ladder bot here: my PlayStation login runs out in about {max(0, round(days_left))} days. "
                                           "Ask Claude to walk you through refreshing it (takes 2 minutes).")
            meta["login_warned"] = True
    meta["status"] = "ok"
    meta.pop("error", None)
    meta["last_run"] = now
    return state


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fake", help="JSON file with a pretend chat")
    ap.add_argument("--state", default=str(ROOT / "data" / "state.json"))
    ap.add_argument("--site", default=str(ROOT / "docs"))
    ap.add_argument("--now", type=int, help="pretend current time (ms), for testing")
    args = ap.parse_args()

    cfg = load_config()
    state_path = Path(args.state)
    state = load_state(state_path)
    now = args.now or int(time.time() * 1000)
    npsso = os.environ.get("PSN_NPSSO", "").strip()
    code = 0
    try:
        if args.fake:
            from .chat import FakeChat
            chat = FakeChat(args.fake)
        else:
            if not npsso:
                raise RuntimeError("PSN_NPSSO is not set")
            from .chat import PSNChat
            chat = PSNChat(npsso, cfg.get("group_id", ""), cfg.get("group_name", ""))
        run(chat, state, cfg, now, npsso)
        if args.fake:
            for text in chat.sent:
                print("BOT >", text.replace("\n", "\n      "), "\n")
    except Exception as exc:  # keep the website up even if PlayStation is unhappy
        msg = f"{type(exc).__name__}: {exc}"
        auth = "auth" in msg.lower() or "npsso" in msg.lower() or "401" in msg
        state["meta"].update(status="login_failed" if auth else "error", error=msg[:300], last_error=now)
        traceback.print_exc()
        code = 1 if args.fake else 0  # errors show on the website instead of failing the run (no GitHub emails)
    save_state(state_path, state)
    build_site(Ladder(state, admin_name=cfg.get("admin_name") or cfg.get("admin_psn") or "the admin", site_url=cfg.get("site_url", "")).snapshot(now),
               Path(args.site))
    return code


if __name__ == "__main__":
    sys.exit(main())
