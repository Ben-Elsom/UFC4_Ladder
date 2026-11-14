"""Always-on mode for the server: check the chat every few seconds and reply straight away.

    PSN_NPSSO=... python -m ladder.serve

Saves state locally after every change and pushes it (plus the website) to GitHub,
at most once a minute, and at least every 30 minutes so the site shows it's alive.
Admin fixes made on the server with `python -m ladder.admin` are picked up automatically.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import traceback
from pathlib import Path

from .engine import Ladder
from .run import ROOT, load_config, load_state, run, save_state
from .site import build_site

POLL_SECONDS = 8
PUSH_MIN_GAP = 60
HEARTBEAT_PUSH = 30 * 60
MEMBER_REFRESH = 10 * 60
STATE = ROOT / "data" / "state.json"
SITE = ROOT / "docs"


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def git(*args: str) -> bool:
    r = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)
    if r.returncode != 0:
        log(f"git {' '.join(args)} failed: {r.stderr.strip()[:300]}")
    return r.returncode == 0


def publish() -> None:
    git("add", "data", "docs")
    if subprocess.run(["git", "-C", str(ROOT), "diff", "--cached", "--quiet"]).returncode == 0:
        return
    git("commit", "-q", "-m", "Ladder update")
    if not git("pull", "--rebase", "-q", "-X", "theirs"):
        git("rebase", "--abort")
    git("push", "-q")


def fingerprint(state: dict) -> str:
    meta = {k: v for k, v in state["meta"].items() if k not in ("last_run", "login_days_left")}
    return json.dumps({**state, "meta": meta}, sort_keys=True)


def main() -> None:
    npsso = os.environ.get("PSN_NPSSO", "").strip()
    if not npsso:
        raise SystemExit("PSN_NPSSO is not set")
    from .chat import PSNChat

    cfg = load_config()
    state = load_state(STATE)
    mtime = STATE.stat().st_mtime if STATE.exists() else 0
    chat = None
    last_fp, last_push, last_members, cfg_at = fingerprint(state), 0.0, 0.0, time.time()
    pending_push = False
    backoff = POLL_SECONDS

    while True:
        now_s = time.time()
        try:
            if STATE.exists() and STATE.stat().st_mtime != mtime:   # admin fix made on the server
                state = load_state(STATE)
                log("state changed on disk; reloaded")
                pending_push = True
            if now_s - cfg_at > 300:
                cfg, cfg_at = load_config(), now_s
            if chat is None:
                chat = PSNChat(npsso, cfg.get("group_id", ""), cfg.get("group_name", ""))
                log(f"logged in as {chat.identity()[1]}, watching '{chat.group_label()[1]}'")
            if now_s - last_members > MEMBER_REFRESH:
                chat._info = None
                last_members = now_s
            before = len(state["log"])
            run(chat, state, cfg, int(now_s * 1000), npsso)
            for entry in state["log"][before:]:
                log("replied: " + entry["text"].splitlines()[0])
            backoff = POLL_SECONDS
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"
            auth = "auth" in msg.lower() or "npsso" in msg.lower() or "401" in msg
            state["meta"].update(status="login_failed" if auth else "error", error=msg[:300], last_error=int(now_s * 1000))
            log("error: " + msg)
            traceback.print_exc()
            chat = None                       # log in again next time
            backoff = min(600, max(30, backoff * 2))

        fp = fingerprint(state)
        changed = fp != last_fp
        if changed or pending_push or now_s - last_push > HEARTBEAT_PUSH:
            save_state(STATE, state)
            mtime = STATE.stat().st_mtime
            last_fp = fp
            pending_push = pending_push or changed
            if now_s - last_push >= PUSH_MIN_GAP or now_s - last_push > HEARTBEAT_PUSH:
                build_site(Ladder(state, admin_name=cfg.get("admin_name") or cfg.get("admin_psn") or "the admin",
                                  site_url=cfg.get("site_url", "")).snapshot(int(now_s * 1000)), SITE)
                publish()
                last_push, pending_push = now_s, False
        time.sleep(backoff)


if __name__ == "__main__":
    main()
