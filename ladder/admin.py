"""Admin fixes (run by Claude when the admin asks). Ratings rebuild themselves from the log.

    python -m ladder.admin list
    python -m ladder.admin void 14 "wrong opponent"
    python -m ladder.admin restore 14
    python -m ladder.admin edit 14 --score 2-0 --mode sb --winner jono --loser sam --method KO
    python -m ladder.admin swap 14
    python -m ladder.admin add "jono beat sam 2-1 mma" [--when 2026-11-14T20:30]
    python -m ladder.admin resolve 3 "added as #15"
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime
from pathlib import Path

from .engine import MODES, Ladder
from .names import resolve
from .parser import SELF, detect_mode, parse
from .run import ROOT, load_config, load_state, save_state
from .site import build_site


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["list", "void", "restore", "edit", "swap", "add", "resolve"])
    ap.add_argument("target", nargs="?")
    ap.add_argument("note", nargs="?", default="")
    ap.add_argument("--winner"); ap.add_argument("--loser"); ap.add_argument("--score"); ap.add_argument("--mode"); ap.add_argument("--method")
    ap.add_argument("--when", help="local time of the fight, e.g. 2026-11-14T20:30 (default now)")
    ap.add_argument("--state", default=str(ROOT / "data" / "state.json"))
    a = ap.parse_args()

    cfg = load_config()
    path = Path(a.state)
    state = load_state(path)
    lad = Ladder(state, admin_name=cfg.get("admin_name") or cfg.get("admin_psn") or "the admin", site_url=cfg.get("site_url", ""))
    pool = {acct: p["name"] for acct, p in state["players"].items()}
    now = int(time.time() * 1000)

    def who(text: str) -> str:
        status, ids = resolve(text, pool)
        if status != "ok":
            raise SystemExit(f"Can't pin down '{text}': {[pool[i] for i in ids] or 'no match'}")
        return ids[0]

    def fight(fid: str) -> dict:
        f = next((x for x in state["fights"] if x["id"] == int(fid.lstrip("#"))), None)
        if not f:
            raise SystemExit(f"No fight #{fid}")
        return f

    def close_reviews(fid: int, note: str) -> None:
        for r in state["review"]:
            if r.get("fight_id") == fid and not r["resolved"]:
                r["resolved"], r["resolution"] = True, note

    if a.action == "list":
        for f in state["fights"]:
            if f["status"] == "disputed":
                print(f"PAUSED #{f['id']} {MODES[f['mode']]}: {lad._describe(f)}  <- {f['text']!r}")
        for r in state["review"]:
            if not r["resolved"]:
                print(f"R{r['id']} [{r['kind']}] {r['reason']} | {lad.name(r['sender']) if r.get('sender') else ''}: {r['text']!r}")
        for q in state["pending"]:
            print(f"WAITING {lad.name(q['sender'])} ({q['kind']}): {q['text']!r}")
        return
    if a.action == "void":
        f = fight(a.target); f["status"] = "void"; f["note"] = a.note or "removed by admin"
        close_reviews(f["id"], f"#{f['id']} removed")
    elif a.action == "restore":
        f = fight(a.target); f["status"] = "ok"; f.pop("note", None)
        close_reviews(f["id"], f"#{f['id']} kept")
    elif a.action == "swap":
        f = fight(a.target); f["winner"], f["loser"] = f["loser"], f["winner"]; f["status"] = "ok"
        close_reviews(f["id"], f"#{f['id']} winner swapped")
    elif a.action == "edit":
        f = fight(a.target)
        if a.winner: f["winner"] = who(a.winner)
        if a.loser: f["loser"] = who(a.loser)
        if a.score:
            x, y = sorted((int(n) for n in a.score.replace("–", "-").split("-")), reverse=True); f["score"] = [x, y]
        if a.mode: f["mode"] = detect_mode(a.mode) or a.mode
        if a.method: f["method"] = a.method.upper()
        f["status"] = "ok"
        close_reviews(f["id"], f"#{f['id']} corrected")
    elif a.action == "add":
        rep = parse(a.target)
        if not rep or SELF in (rep.winner, rep.loser) or not rep.mode:
            raise SystemExit("Write it as: <winner> beat <loser> [score] <mma|sb>")
        ts = int(datetime.fromisoformat(a.when).timestamp() * 1000) if a.when else now
        w, l = who(rep.winner), who(rep.loser)
        fid = state["next_fight_id"]; state["next_fight_id"] += 1
        state["fights"].append({"id": fid, "mode": rep.mode, "winner": w, "loser": l, "score": list(rep.score) if rep.score else None,
                                "method": rep.method, "reported_by": w, "ts": ts, "msg_uid": f"admin#{now}", "text": a.target,
                                "status": "ok", "note": "added by admin"})
        print(f"Added #{fid}")
    elif a.action == "resolve":
        r = next((x for x in state["review"] if x["id"] == int(a.target.lstrip("Rr"))), None)
        if not r:
            raise SystemExit(f"No review item R{a.target}")
        r["resolved"], r["resolution"] = True, a.note or "sorted by admin"

    save_state(path, state)
    build_site(lad.snapshot(now), ROOT / "docs")
    print("Saved. Ratings and website rebuilt.")


if __name__ == "__main__":
    main()
