"""Result fixes shared by the admin web page and the `ladder.admin` command line.

Every function changes the state in place. Ratings rebuild themselves from the fight log,
so nothing else needs updating. Bad input raises AdminError with a message for the admin.
"""

from __future__ import annotations

import copy
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


def _check(state: dict, w: str | None, lo: str | None, score: list[int] | None, mode: str | None) -> None:
    for acct in (w, lo):
        if not acct or acct not in state["players"]:
            raise AdminError("Pick both players from the list")
    if w == lo:
        raise AdminError("The winner and loser can't be the same person")
    if score is not None:
        a, b = (int(n) for n in score)
        if a < 0 or b < 0 or a > 20 or b > 20:
            raise AdminError("Scores must be between 0 and 20")
        if a == 0 and b == 0:
            raise AdminError("The score can't be 0-0")
        if a < b:
            raise AdminError("The winner's score goes first and can't be lower than the loser's")
    if mode is not None and mode not in MODES:
        raise AdminError("Mode must be MMA or Stand & Bang")


def edit(state: dict, fid: int | str, *, winner: str | None = None, loser: str | None = None,
         score: list[int] | None | str = "keep", mode: str | None = None, method: str | None = None) -> dict:
    """Change any part of a result. score="keep" leaves it alone; None clears it.

    The score is the winner's games first. Equal numbers mean a split."""
    f = find_fight(state, fid)
    w = winner or f["winner"]
    lo = loser or f["loser"]
    _check(state, w, lo, None if score == "keep" else score, mode)

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


def correction(state: dict, fid: int | str, was: str | None, make_ladder) -> str:
    """Queue a result message for the group chat (state["outbox"]; the bot posts it on its next check).
    With `was`, it's a correction after an edit; without, the result is just posted again."""
    f = find_fight(state, fid)
    if f["status"] == "void":
        raise AdminError("This result is removed, so there's nothing to resend")
    lad = make_ladder(state)
    head = (f"🎙️ WE HAVE A CORRECTION\n- {MODES[f['mode']]} result, was: {was}" if was else
            f"🔁 {MODES[f['mode']]} result (resent)")
    text = lad._confirm(f, head)
    state.setdefault("outbox", []).append(text)
    return text


def describe(state: dict, f: dict, make_ladder) -> str:
    return make_ladder(state)._describe(f)


def resolve_review(state: dict, rid: int | str, note: str = "") -> dict:
    r = find_review(state, rid)
    r["resolved"], r["resolution"] = True, note or "sorted by admin"
    return r


def find_review(state: dict, rid: int | str) -> dict:
    try:
        n = int(str(rid).lstrip("Rr"))
    except ValueError:
        raise AdminError(f"No flagged message {rid}") from None
    r = next((x for x in state["review"] if x["id"] == n), None)
    if r is None:
        raise AdminError(f"No flagged message {rid}")
    return r


def add_result(state: dict, *, winner: str, loser: str, score: list[int] | None, mode: str | None,
               ts: int | None = None, text: str = "", reported_by: str | None = None, uid: str = "") -> dict:
    """A result the admin adds by hand (e.g. finishing off a flagged message). Not announced in the chat."""
    if not mode:
        raise AdminError("Pick the mode")
    _check(state, winner, loser, score, mode)
    now = _now()
    fid = state["next_fight_id"]
    state["next_fight_id"] += 1
    f = {"id": fid, "mode": mode, "winner": winner, "loser": loser, "score": [int(n) for n in score] if score else None,
         "method": None, "reported_by": reported_by or winner, "ts": ts or now, "msg_uid": uid or f"admin#{now}#{fid}",
         "text": text, "status": "ok", "note": "added by admin"}
    state["fights"].append(f)
    return f


# ---------------------------------------------------------------- flagged messages
def _rows_from_question(q: dict) -> list[dict]:
    """Turn a half-finished bot question into result rows, with None for whatever is still missing."""
    k = q.get("kind")
    if k == "conflict":
        return []
    if k == "batch":
        rows = []
        for d in q.get("drafts", []):
            hi, lo = d["score"]
            s, o = q["sender"], d.get("opp")
            w, l = (s, o) if d.get("won") or hi == lo else (o, s)
            rows.append({"winner": w, "loser": l, "score": [hi, lo], "mode": d.get("mode"), "text": d.get("text") or q.get("text", ""),
                         "options": d.get("options") or []})
        return rows
    if k == "who":
        m, t = q["mine"], q["theirs"]
        w, l = (q["sender"], None) if m >= t else (None, q["sender"])
        return [{"winner": w, "loser": l, "score": [max(m, t), min(m, t)], "mode": q.get("mode"), "text": q.get("text", ""), "options": []}]
    if "winner" in q:
        return [{"winner": q.get("winner"), "loser": q.get("loser"), "score": list(q["score"]) if q.get("score") else None,
                 "mode": q.get("mode"), "text": q.get("text", ""), "options": q.get("options") or []}]
    return []


def guess_rows(state: dict, r: dict, make_ladder) -> list[dict]:
    """Best guess at the result(s) a flagged message was about, for the admin to finish off.

    Uses the question the bot saved when it flagged it. Older flags didn't save one, so the
    original message is replayed through a throwaway copy of the bot to see what it understood."""
    if r.get("fight_id"):
        return []
    if r.get("question"):
        rows = _rows_from_question(r["question"])
        if rows:
            return rows
    blank = [{"winner": None, "loser": None, "score": None, "mode": None, "text": r.get("text", ""), "options": []}]
    sender, text = r.get("sender"), r.get("text")
    if not sender or not text or sender not in state["players"]:
        return blank
    sim = copy.deepcopy(state)
    sim["pending"] = [q for q in sim["pending"] if q["sender"] != sender and sender not in q.get("players", ())]
    n_f, n_r = len(sim["fights"]), len(sim["review"])
    try:
        make_ladder(sim).handle({"uid": f"admin-guess-{r['id']}", "sender_id": sender, "body": text,
                                 "sender_name": sim["players"][sender]["name"], "ts": r.get("ts") or _now()})
    except Exception:
        return blank
    new_fights = sim["fights"][n_f:]
    if new_fights:
        return [{"winner": f["winner"], "loser": f["loser"], "score": f.get("score"), "mode": f["mode"],
                 "text": f.get("text", ""), "options": []} for f in new_fights]
    q = next((q for q in sim["pending"] if q["sender"] == sender), None)
    if q:
        return _rows_from_question(q) or blank
    for nr in reversed(sim["review"][n_r:]):
        if nr.get("question"):
            return _rows_from_question(nr["question"]) or blank
    return blank


def reask(state: dict, rid: int | str, make_ladder, now: int | None = None) -> str:
    """Put a flagged message through the bot again as if just posted, so it asks its question in the chat
    again (with a fresh 5 minutes). The replies go into state["outbox"]; the bot posts them on its next check."""
    r = find_review(state, rid)
    if r["resolved"]:
        raise AdminError("That one's already sorted")
    if r.get("fight_id"):
        raise AdminError("This one is about an existing result. Use Edit on that result instead.")
    sender, text = r.get("sender"), r.get("text")
    if not sender or not text or sender not in state["players"]:
        raise AdminError("There's no message to ask about again")
    name = state["players"][sender]["name"]
    if any(q["sender"] == sender or sender in q.get("players", ()) for q in state["pending"]):
        raise AdminError(f"{name} already has a question waiting in the chat. Try again once that's answered.")
    now = now or _now()
    before = len(state["fights"])
    replies = make_ladder(state).handle({"uid": f"reask-{r['id']}-{now}", "sender_id": sender, "sender_name": name,
                                         "body": text, "ts": now})
    if not replies:
        raise AdminError("The bot couldn't find a result in that message. Fill in the result above instead.")
    asked = any(q["sender"] == sender for q in state["pending"])
    if asked:
        replies[0] = "🔁 Asking again\n" + replies[0]
    state.setdefault("outbox", []).extend(replies)
    r["resolved"], r["resolution"] = True, "asked again in the chat"
    if len(state["fights"]) > before:
        return f"The bot understood it this time and added result {state['fights'][-1]['id']}. It'll post the confirmation in the chat."
    return f"Asked {name} again in the group chat. It'll post within a few seconds." if asked else \
        "Sent to the chat again. The bot will post its reply within a few seconds."
