"""Result fixes shared by the admin web page and the `ladder.admin` command line.

Every function changes the state in place. Ratings rebuild themselves from the fight log,
so nothing else needs updating. Bad input raises AdminError with a message for the admin.
"""

from __future__ import annotations

import time

from .engine import MODES


class AdminError(ValueError):
    pass


def _now() -> int:
    return int(time.time() * 1000)


def find_fight(state: dict, fid: int | str) -> dict:
    try:
        n = int(str(fid).lstrip("#"))
    except ValueError:
        raise AdminError(f"No result {fid}") from None
    f = next((x for x in state["fights"] if x["id"] == n), None)
    if f is None:
        raise AdminError(f"No result {fid}")
    return f


def close_reviews(state: dict, fid: int, note: str) -> None:
    for r in state["review"]:
        if r.get("fight_id") == fid and not r["resolved"]:
            r["resolved"], r["resolution"] = True, note


def _history(f: dict, action: str) -> None:
    f.setdefault("admin_edits", []).append({
        "ts": _now(), "action": action,
        "was": {k: f.get(k) for k in ("winner", "loser", "score", "mode", "status")},
    })


def void(state: dict, fid: int | str, note: str = "") -> dict:
    f = find_fight(state, fid)
    _history(f, "removed")
    f["status"], f["note"] = "void", note or "removed by admin"
    close_reviews(state, f["id"], f"#{f['id']} removed")
    return f


def restore(state: dict, fid: int | str) -> dict:
    f = find_fight(state, fid)
    _history(f, "restored")
    f["status"] = "ok"
    f.pop("note", None)
    close_reviews(state, f["id"], f"#{f['id']} kept")
    return f


def swap(state: dict, fid: int | str) -> dict:
    f = find_fight(state, fid)
    return edit(state, f["id"], winner=f["loser"], loser=f["winner"])


def parse_score(text: str) -> list[int] | None:
    text = (text or "").strip().replace("–", "-")
    if not text:
        return None
    try:
        a, b = (int(n) for n in text.split("-"))
    except ValueError:
        raise AdminError(f"Score should look like 2-1, not \"{text}\"") from None
    return [a, b]


def edit(state: dict, fid: int | str, *, winner: str | None = None, loser: str | None = None,
         score: list[int] | None | str = "keep", mode: str | None = None, method: str | None = None) -> dict:
    """Change any part of a result. score="keep" leaves it alone; None clears it.

    The score is the winner's games first. Equal numbers mean a split."""
    f = find_fight(state, fid)
    w = winner or f["winner"]
    lo = loser or f["loser"]
    for acct in (w, lo):
        if acct not in state["players"]:
            raise AdminError("Pick both players from the list")
    if w == lo:
        raise AdminError("The winner and loser can't be the same person")
    if score != "keep" and score is not None:
        a, b = (int(n) for n in score)
        if a < 0 or b < 0 or a > 20 or b > 20:
            raise AdminError("Scores must be between 0 and 20")
        if a == 0 and b == 0:
            raise AdminError("The score can't be 0-0")
        if a < b:
            raise AdminError("The winner's score goes first and can't be lower than the loser's")
    if mode is not None and mode not in MODES:
        raise AdminError("Mode must be MMA or Stand & Bang")

    _history(f, "edited")
    f["winner"], f["loser"] = w, lo
    if score != "keep":
        f["score"] = [int(n) for n in score] if score is not None else None
    if mode is not None:
        f["mode"] = mode
    if method:
        f["method"] = method.upper()
    f["status"] = "ok" if f["status"] != "void" else "void"
    close_reviews(state, f["id"], f"#{f['id']} corrected")
    return f


def resolve_review(state: dict, rid: int | str, note: str = "") -> dict:
    try:
        n = int(str(rid).lstrip("Rr"))
    except ValueError:
        raise AdminError(f"No flagged message {rid}") from None
    r = next((x for x in state["review"] if x["id"] == n), None)
    if r is None:
        raise AdminError(f"No flagged message {rid}")
    r["resolved"], r["resolution"] = True, note or "sorted by admin"
    return r
