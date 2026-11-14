"""The ladder itself: reads chat messages, keeps the fight log, works out ratings.

Everything is rebuilt from the fight log each time, so fixing or removing a
fight is just changing that one entry.
"""

from __future__ import annotations

import re
from typing import Any

from . import glicko
from .names import norm, resolve as _fuzzy_resolve
from .parser import SELF, VsLine, detect_mode, has_score, parse, parse_bare, parse_vs_lines

MODES = {"mma": "MMA", "sb": "Stand & Bang"}
PROVISIONAL_FIGHTS = 5     # fights before you appear on the board
DUPLICATE_MINUTES = 10     # both players reporting the same result within this window = one result
SET_UPDATE_MINUTES = 60    # 1-1 then 2-1 within this long of the last post = the same set, updated
MODE_GUESS_HOURS = 6       # no mode given? use what the pair last played within this window
QUESTION_EXPIRES_HOURS = 24
MIN = 60 * 1000
H = 60 * MIN
DAY = 24 * H


def new_state() -> dict[str, Any]:
    return {
        "version": 1,
        "bot": {},
        "players": {},
        "seen": [],
        "fights": [],
        "pending": [],
        "review": [],
        "log": [],
        "next_fight_id": 1,
        "next_review_id": 1,
        "meta": {},
    }


class Ladder:
    def __init__(self, state: dict[str, Any], admin_name: str = "the admin", site_url: str = "",
                 nicknames: dict[str, str] | None = None):
        self.s = state
        # Admin-set defaults, e.g. {"maori": "maoripunisher_"}: that word always means that player.
        self.nicknames = {norm(k): v.lower() for k, v in (nicknames or {}).items()}
        self.admin = admin_name
        self.site = site_url
        self._seen = set(state["seen"])

    # ------------------------------------------------------------ players
    def name(self, acct: str) -> str:
        return self.s["players"].get(acct, {}).get("name", "unknown")

    def sync_members(self, members: list[tuple[str, str]]) -> None:
        bot = self.s["bot"].get("account_id")
        for acct, online_id in members:
            if acct == bot:
                continue
            p = self.s["players"].setdefault(acct, {"name": online_id})
            p["name"] = online_id
            p["in_group"] = True
        current = {a for a, _ in members}
        for acct, p in self.s["players"].items():
            if acct not in current:
                p["in_group"] = False

    def _resolve(self, text: str, pool: dict[str, str], exclude: set[str] = frozenset(),
                 prefer: list[str] | None = None) -> tuple[str, list[str]]:
        target = self.nicknames.get(norm(text))
        if target:
            for acct, name in pool.items():
                if name.lower() == target and acct not in exclude:
                    return "ok", [acct]
        return _fuzzy_resolve(text, pool, exclude=exclude, prefer=prefer)

    def _name_pool(self) -> dict[str, str]:
        """People a name could mean: current members, plus ex-members who already have fights logged."""
        fought = {x for f in self.s["fights"] if f["status"] != "void" for x in (f["winner"], f["loser"])}
        return {a: p["name"] for a, p in self.s["players"].items() if p.get("in_group", True) or a in fought}

    def _recent_opponents(self, acct: str) -> list[str]:
        out: list[str] = []
        for f in reversed(self.s["fights"]):
            if f["status"] == "void" or acct not in (f["winner"], f["loser"]):
                continue
            opp = f["loser"] if f["winner"] == acct else f["winner"]
            if opp not in out:
                out.append(opp)
        return out

    # ------------------------------------------------------------ ratings
    def replay(self, mode: str, now: int | None = None) -> dict[str, Any]:
        stats: dict[str, dict[str, Any]] = {}
        changes: dict[int, dict[str, tuple[float, float]]] = {}

        def st(acct: str) -> dict[str, Any]:
            return stats.setdefault(acct, {
                "rating": glicko.START_RATING, "rd": glicko.START_RD, "vol": glicko.START_VOL,
                "last": None, "fights": 0, "wins": 0, "losses": 0, "sets_won": 0, "sets_lost": 0,
                "streak": 0, "best_rating": glicko.START_RATING,
            })

        def ranked() -> list[str]:
            return sorted((a for a, x in stats.items() if x["fights"] >= PROVISIONAL_FIGHTS), key=lambda a: -stats[a]["rating"])

        for f in sorted(self.s["fights"], key=lambda f: f["ts"]):
            if f["mode"] != mode or f["status"] != "ok":
                continue
            w, l = st(f["winner"]), st(f["loser"])
            for x in (w, l):
                if x["last"] is not None:
                    x["rd"] = glicko.inflate(x["rd"], x["vol"], (f["ts"] - x["last"]) / DAY)
            gw, gl = f.get("score") or (1, 0)
            w_res = [(l["rating"], l["rd"], 1.0)] * gw + [(l["rating"], l["rd"], 0.0)] * gl
            l_res = [(w["rating"], w["rd"], 0.0)] * gw + [(w["rating"], w["rd"], 1.0)] * gl
            before = (w["rating"], l["rating"])
            w["rating"], w["rd"], w["vol"] = glicko.update(w["rating"], w["rd"], w["vol"], w_res)
            l["rating"], l["rd"], l["vol"] = glicko.update(l["rating"], l["rd"], l["vol"], l_res)
            changes[f["id"]] = {"winner": (before[0], w["rating"]), "loser": (before[1], l["rating"])}
            w["fights"] += gw + gl
            l["fights"] += gw + gl
            w["wins"] += gw; w["losses"] += gl
            l["wins"] += gl; l["losses"] += gw
            if gw != gl:   # a split (2-2) counts every fight but isn't a set win or loss
                w["sets_won"] += 1; l["sets_lost"] += 1
                w["streak"] = w["streak"] + 1 if w["streak"] > 0 else 1
                l["streak"] = l["streak"] - 1 if l["streak"] < 0 else -1
            w["last"] = l["last"] = f["ts"]
            w["best_rating"] = max(w["best_rating"], w["rating"])

        board = ranked()
        for acct, x in stats.items():
            x["rank"] = board.index(acct) + 1 if acct in board else None
            x["provisional"] = x["fights"] < PROVISIONAL_FIGHTS
        return {"stats": stats, "board": board, "changes": changes}

    # ------------------------------------------------------------ chat handling
    def handle(self, msg: dict[str, Any]) -> list[str]:
        """msg: uid, sender_id, sender_name, body, ts (ms). Returns replies to post."""
        if msg["uid"] in self._seen:
            return []
        self._seen.add(msg["uid"])
        self.s["seen"].append(msg["uid"])
        self.s["seen"] = self.s["seen"][-3000:]
        if msg["sender_id"] == self.s["bot"].get("account_id"):
            return []
        p = self.s["players"].setdefault(msg["sender_id"], {"name": msg["sender_name"], "in_group": True})
        p["name"] = msg["sender_name"] or p["name"]

        body = (msg.get("body") or "").strip()
        if not body:
            return []
        low = body.lower()
        if low.startswith("!"):
            return self._command(msg, low[1:].split())

        pending = next((q for q in self.s["pending"] if q["sender"] == msg["sender_id"]), None)
        if pending:
            answer = self._answer(pending, body, msg)
            if answer is not None:
                return answer

        # The score is the key: a message without one is just chat.
        if not has_score(body):
            return []

        vs = parse_vs_lines(body)
        if vs:
            return self._vs_batch(msg, body, vs)

        rep = parse(body)
        if rep is not None and rep.score is not None and not rep.implicit:
            draft = {
                "sender": msg["sender_id"], "uid": msg["uid"], "ts": msg["ts"], "text": body,
                "winner": None, "loser": None, "score": rep.score, "mode": rep.mode, "method": None,
            }
            return self._fill_names(draft, rep.winner, rep.loser, True)
        return self._bare_score(msg, body)

    # ------------------------------------------------------------ a score with no clear opponent
    def _bare_score(self, msg: dict, body: str) -> list[str]:
        b = parse_bare(body)
        if b is None:
            return []
        sender = msg["sender_id"]
        if b.name:
            status, ids = self._resolve(b.name, self._name_pool(), exclude={sender}, prefer=self._recent_opponents(sender))
            if status != "none":
                return self._vs_batch(msg, body, [VsLine(name=b.name, mine=b.mine, theirs=b.theirs, mode=b.mode, text=b.text)])
        # No name: is it the set they posted earlier, carrying on? (1-1 earlier, now 2-1)
        f = self._latest_set_of(sender, msg["ts"], b.mode)
        if f:
            opp = f["loser"] if f["winner"] == sender else f["winner"]
            mine = self._games(f, sender)
            theirs = self._games(f, opp)
            if b.mine >= mine and b.theirs >= theirs and b.mine + b.theirs > mine + theirs:
                d = {"sender": sender, "uid": msg["uid"], "ts": msg["ts"], "text": body, "mode": f["mode"], "method": None,
                     "winner": sender if b.mine >= b.theirs else opp, "loser": opp if b.mine >= b.theirs else sender,
                     "score": (max(b.mine, b.theirs), min(b.mine, b.theirs))}
                return self._finalise(d)
        q = {"sender": sender, "uid": msg["uid"], "ts": msg["ts"], "text": body,
             "mine": b.mine, "theirs": b.theirs, "mode": b.mode}
        self._set_pending(q, "who")
        meaning = "you won" if b.mine > b.theirs else "you lost" if b.mine < b.theirs else "it was a split"
        hint = "" if b.mine == b.theirs else f" If not, post it again like: lost to bjkid {min(b.mine, b.theirs)}-{max(b.mine, b.theirs)}"
        return [f"🤔 Who was that {b.mine}-{b.theirs} against? Reply with their name. "
                f"(Your score goes first, so {meaning}.{hint})"]

    def _latest_set_of(self, acct: str, now: int, mode: str | None) -> dict | None:
        for f in reversed(self.s["fights"]):
            if f["status"] == "void" or acct not in (f["winner"], f["loser"]):
                continue
            if mode and f["mode"] != mode:
                continue
            return f if now - f.get("updated_ts", f["ts"]) <= SET_UPDATE_MINUTES * MIN else None
        return None

    def _fill_names(self, d: dict, w_text: str, l_text: str, strong: bool) -> list[str]:
        sender = d["sender"]
        pool = self._name_pool()
        prefer = self._recent_opponents(sender)
        for side, text in (("winner", w_text), ("loser", l_text)):
            if text == SELF:
                d[side] = sender
        for side, text in (("winner", w_text), ("loser", l_text)):
            if text == SELF:
                continue
            other = d["loser" if side == "winner" else "winner"]
            exclude = {other} if other else set()
            if other is None and SELF in (w_text, l_text):
                exclude = {sender}
            status, ids = self._resolve(text, pool, exclude=exclude, prefer=prefer)
            if status == "ok":
                d[side] = ids[0]
            elif status == "ask":
                d["ask_side"], d["options"], d["other_text"] = side, ids, (l_text if side == "winner" else w_text)
                self._set_pending(d, "name")
                opts = "  ".join(f"{i}) {self.name(a)}" for i, a in enumerate(ids, 1))
                return [f"🤔 Which one did you mean? {opts}\nReply with the number."]
            else:
                if not strong:
                    return []  # probably just chat
                self._add_review("unreadable", f"Couldn't work out who \"{text}\" is", d)
                return [f"❓ Couldn't tell who \"{text}\" is. Post it again using part of their PSN name."]
        if d["winner"] == d["loser"]:
            return []
        return self._fill_mode(d)

    def _fill_mode(self, d: dict) -> list[str]:
        if d["mode"] is None:
            pair = {d["winner"], d["loser"]}
            for f in reversed(self.s["fights"]):
                if f["status"] != "void" and {f["winner"], f["loser"]} == pair and d["ts"] - f["ts"] <= MODE_GUESS_HOURS * H:
                    d["mode"] = f["mode"]
                    break
        if d["mode"] is None:
            self._set_pending(d, "mode")
            return [f"🥊 {self.name(d['winner'])} def. {self.name(d['loser'])}: MMA or Stand & Bang? Reply mma or sb."]
        return self._finalise(d)

    def _set_pending(self, d: dict, kind: str) -> None:
        for old in [q for q in self.s["pending"] if q["sender"] == d["sender"]]:
            self.s["pending"].remove(old)
            self._add_review("unanswered", "Question never answered (they posted another result)", old)
        d["kind"] = kind
        d["asked_at"] = d.get("asked_at") or d["ts"]
        self.s["pending"].append(d)

    def _answer(self, q: dict, body: str, msg: dict) -> list[str] | None:
        words = body.lower().split()
        if q["kind"] == "batch":
            return self._answer_batch(q, body, words)
        if q["kind"] == "who":
            name = " ".join(w for w in words if w not in ("vs", "v", "against", "it", "was", "with"))
            if not name or len(words) > 4 or has_score(body):
                return None
            status, ids = self._resolve(name, self._name_pool(), exclude={q["sender"]}, prefer=self._recent_opponents(q["sender"]))
            if status == "none":
                return [f"❓ Couldn't tell who \"{name}\" is. Reply with part of their PSN name."]
            self.s["pending"].remove(q)
            return self._vs_batch(msg | {"uid": q["uid"], "ts": q["ts"]}, q["text"],
                                  [VsLine(name=self.name(ids[0]) if status == "ok" else name,
                                          mine=q["mine"], theirs=q["theirs"], mode=q["mode"], text=q["text"])])
        if q["kind"] == "mode":
            mode = detect_mode(body)
            if mode and len(words) <= 4:
                self.s["pending"].remove(q)
                q["mode"] = mode
                return self._finalise(q)
            return None
        # kind == name
        pick = None
        if len(words) == 1 and words[0].strip(").") in {"1", "2", "3"}:
            i = int(words[0].strip(").")) - 1
            if i < len(q["options"]):
                pick = q["options"][i]
        elif len(words) <= 2:
            status, ids = self._resolve(body, {a: self.name(a) for a in q["options"]})
            if status == "ok":
                pick = ids[0]
        if pick is None:
            return None
        self.s["pending"].remove(q)
        q[q["ask_side"]] = pick
        other_side = "loser" if q["ask_side"] == "winner" else "winner"
        if q[other_side] is None:
            w = SELF if q["winner"] == q["sender"] else (self.name(q["winner"]) if q["winner"] else q["other_text"])
            l = SELF if q["loser"] == q["sender"] else (self.name(q["loser"]) if q["loser"] else q["other_text"])
            q["asked_at"] = None
            return self._fill_names(q, w, l, True)
        return self._fill_mode(q)

    # ------------------------------------------------------------ "vs name score" messages
    def _vs_batch(self, msg: dict, body: str, lines: list) -> list[str]:
        """One message with one or more 'vs name a-b' lines. First number is the poster's."""
        sender = msg["sender_id"]
        pool, prefer = self._name_pool(), self._recent_opponents(sender)
        whole_mode = detect_mode(body)
        drafts, notes = [], []
        for i, ln in enumerate(lines):
            if ln.mine == ln.theirs == 0:
                continue
            status, ids = self._resolve(ln.name, pool, exclude={sender}, prefer=prefer)
            if status == "none":
                self._add_review("unreadable", f"Couldn't work out who \"{ln.name}\" is", {"text": ln.text, "sender": sender, "ts": msg["ts"]})
                notes.append(f"❓ Couldn't tell who \"{ln.name}\" is. Post that one again using part of their PSN name.")
                continue
            drafts.append({
                "sender": sender, "uid": f"{msg['uid']}#{i}", "ts": msg["ts"], "text": ln.text,
                "won": ln.mine > ln.theirs, "score": (max(ln.mine, ln.theirs), min(ln.mine, ln.theirs)),
                "mode": ln.mode or whole_mode, "method": None,
                "opp": ids[0] if status == "ok" else None, "options": ids if status == "ask" else None,
                "name_text": ln.name,
            })
        if not drafts:
            return notes
        q = {"sender": sender, "uid": msg["uid"], "ts": msg["ts"], "text": body, "drafts": drafts}
        return notes + self._advance_batch(q, new=True)

    def _advance_batch(self, q: dict, new: bool = False) -> list[str]:
        for d in q["drafts"]:
            if d["opp"] is None:
                q["waiting"], q["asking"] = "name", q["drafts"].index(d)
                if new:
                    self._set_pending(q, "batch")
                opts = "  ".join(f"{i}) {self.name(a)}" for i, a in enumerate(d["options"], 1))
                return [f"🤔 Which \"{d['name_text']}\" did you mean? {opts}  (reply with the number)"]
        for d in q["drafts"]:
            if d["mode"] is None:
                for f in reversed(self.s["fights"]):
                    if (f["status"] != "void" and {f["winner"], f["loser"]} == {d["sender"], d["opp"]}
                            and d["ts"] - f["ts"] <= MODE_GUESS_HOURS * H):
                        d["mode"] = f["mode"]
                        break
        if any(d["mode"] is None for d in q["drafts"]):
            q["waiting"] = "mode"
            if new:
                self._set_pending(q, "batch")
            n = sum(1 for d in q["drafts"] if d["mode"] is None)
            return [f"🥊 Got {n} result{'s' if n > 1 else ''}: MMA or Stand & Bang? Reply mma or snb."]
        if q in self.s["pending"]:
            self.s["pending"].remove(q)
        out = []
        for d in q["drafts"]:
            d["winner"], d["loser"] = (d["sender"], d["opp"]) if d["won"] else (d["opp"], d["sender"])
            out += self._finalise(d)
        return ["\n".join(out)] if out else []

    def _answer_batch(self, q: dict, body: str, words: list[str]) -> list[str] | None:
        if q.get("waiting") == "name":
            d = q["drafts"][q["asking"]]
            pick = None
            if len(words) == 1 and words[0].strip(").") in {"1", "2", "3"}:
                i = int(words[0].strip(").")) - 1
                if i < len(d["options"]):
                    pick = d["options"][i]
            elif len(words) <= 2:
                status, ids = self._resolve(body, {a: self.name(a) for a in d["options"]})
                if status == "ok":
                    pick = ids[0]
            if pick is None:
                return None
            d["opp"] = pick
            return self._advance_batch(q)
        mode = detect_mode(body)
        if mode and len(words) <= 4:
            for d in q["drafts"]:
                d["mode"] = d["mode"] or mode
            return self._advance_batch(q)
        return None

    def _games(self, f: dict, acct: str) -> int:
        gw, gl = f.get("score") or (1, 0)
        return gw if f["winner"] == acct else gl

    def _continues(self, d: dict) -> dict | None:
        """Is this the same set carrying on? e.g. 1-1 posted, then 2-1 after the decider.

        Only the latest result between the two, in the same mode, posted (or last updated)
        within SET_UPDATE_MINUTES, where neither player's tally went down and at least one went up.
        """
        pair = {d["winner"], d["loser"]}
        dw, dl = d["score"] or (1, 0)
        new = {d["winner"]: dw, d["loser"]: dl}
        for f in reversed(self.s["fights"]):
            if f["status"] == "void" or f["mode"] != d["mode"] or {f["winner"], f["loser"]} != pair:
                continue
            if d["ts"] - f.get("updated_ts", f["ts"]) > SET_UPDATE_MINUTES * MIN:
                return None
            old = {a: self._games(f, a) for a in pair}
            if all(new[a] >= old[a] for a in pair) and sum(new.values()) > sum(old.values()):
                return f
            return None
        return None

    def _update_set(self, f: dict, d: dict) -> list[str]:
        old = f.get("score") or [1, 0]
        was = f"{old[0]}–{old[1]}"
        f.setdefault("updates", []).append({"ts": d["ts"], "by": d["sender"], "text": d["text"],
                                            "was": {"winner": f["winner"], "loser": f["loser"], "score": f.get("score")}})
        f["winner"], f["loser"] = d["winner"], d["loser"]
        f["score"] = list(d["score"]) if d["score"] else None
        f["updated_ts"] = d["ts"]
        self._resolve_unreadable(d)
        return [self._confirm(f, f"🔄 #{f['id']} {MODES[f['mode']]} updated (was {was})")]

    def _finalise(self, d: dict) -> list[str]:
        carried_on = self._continues(d)
        if carried_on:
            return self._update_set(carried_on, d)
        pair = {d["winner"], d["loser"]}
        for f in reversed(self.s["fights"]):
            if f["status"] == "void" or f["mode"] != d["mode"] or {f["winner"], f["loser"]} != pair:
                continue
            if abs(d["ts"] - f["ts"]) > DUPLICATE_MINUTES * MIN or f["reported_by"] == d["sender"]:
                continue
            split = d["score"] is not None and d["score"][0] == d["score"][1]
            same_winner = f["winner"] == d["winner"] or split
            same_score = f.get("score") is None or d["score"] is None or tuple(f["score"]) == tuple(d["score"])
            if same_winner and same_score:
                f.setdefault("confirmed_by", []).append(d["sender"])
                if f.get("score") is None and d["score"]:
                    f["score"] = list(d["score"])
                f["method"] = f.get("method") or d["method"]
                self._resolve_unreadable(d)
                return [f"👍 Already got that one (#{f['id']}). Thanks for confirming."]
            f["status"] = "disputed"
            self._add_review("conflict", f"Doesn't match #{f['id']} — both players reported different results", d, fight_id=f["id"])
            return [f"⚠️ That doesn't match #{f['id']} ({self._describe(f)}). Both paused, message {self.admin} to sort it."]
        return self._record(d)

    def _record(self, d: dict) -> list[str]:
        mode = d["mode"]
        fid = self.s["next_fight_id"]
        self.s["next_fight_id"] += 1
        fight = {
            "id": fid, "mode": mode, "winner": d["winner"], "loser": d["loser"],
            "score": list(d["score"]) if d["score"] else None, "method": d["method"],
            "reported_by": d["sender"], "ts": d["ts"], "msg_uid": d["uid"], "text": d["text"], "status": "ok",
        }
        self.s["fights"].append(fight)
        self._resolve_unreadable(d)
        return [self._confirm(fight, f"✅ #{fid} {MODES[mode]}")]

    def _confirm(self, fight: dict, head: str) -> str:
        after = self.replay(fight["mode"])
        ch = after["changes"][fight["id"]]

        def fmt(acct: str, pair: tuple[float, float]) -> str:
            delta = round(pair[1]) - round(pair[0])
            x = after["stats"][acct]
            tag = f" ({x['fights']}/{PROVISIONAL_FIGHTS} fights)" if x["provisional"] else ""
            return f"{self.name(acct)} {round(pair[1])} ({'+' if delta >= 0 else '−'}{abs(delta)}){tag}"

        return (f"{head} · {self._describe(fight)} · "
                f"{fmt(fight['winner'], ch['winner'])} · {fmt(fight['loser'], ch['loser'])}")

    def _describe(self, f: dict) -> str:
        sc = f.get("score")
        if sc and sc[0] == sc[1]:
            return f"{self.name(f['winner'])} and {self.name(f['loser'])} split {sc[0]}–{sc[1]}"
        s = f"{self.name(f['winner'])} def. {self.name(f['loser'])}"
        if f.get("score"):
            s += f" {f['score'][0]}–{f['score'][1]}"
        return s

    # ------------------------------------------------------------ review
    def _add_review(self, kind: str, reason: str, d: dict, fight_id: int | None = None) -> None:
        rid = self.s["next_review_id"]
        self.s["next_review_id"] += 1
        self.s["review"].append({
            "id": rid, "kind": kind, "reason": reason, "text": d.get("text", ""), "sender": d.get("sender"),
            "ts": d.get("ts"), "fight_id": fight_id, "resolved": False,
        })

    def _resolve_unreadable(self, d: dict) -> None:
        """A later good report from the same person clears their earlier unreadable one."""
        for r in self.s["review"]:
            if (not r["resolved"] and r["kind"] == "unreadable" and r["sender"] == d["sender"]
                    and 0 <= d["ts"] - (r["ts"] or 0) <= 24 * H):
                r["resolved"] = True
                r["resolution"] = "they reposted it"

    def expire_questions(self, now: int) -> None:
        for q in list(self.s["pending"]):
            if now - (q.get("asked_at") or q["ts"]) > QUESTION_EXPIRES_HOURS * H:
                self.s["pending"].remove(q)
                self._add_review("unanswered", "Bot asked a question that was never answered", q)

    # ------------------------------------------------------------ commands
    def _mode_arg(self, args: list[str]) -> tuple[str | None, list[str]]:
        rest, mode = [], None
        for a in args:
            m = detect_mode(a)
            if m and mode is None:
                mode = m
            else:
                rest.append(a)
        return mode, rest

    def _command(self, msg: dict, parts: list[str]) -> list[str]:
        if not parts:
            return []
        cmd, args = parts[0], parts[1:]
        now = msg["ts"]
        mode, rest = self._mode_arg(args)
        site = f"\nFull stats: {self.site}" if self.site else ""
        if cmd in ("help", "commands", "how"):
            lines = [
                "🥊 POST A RESULT (after you play)",
                "Always put the score in. No score = the bot ignores it.",
                "👉 beat bjkid 2-1 mma",
                "👉 lost to temp 0-1 snb  (one fight = 1-0)",
                "👉 vs bjkid 2-1 mma  (your score first)",
                "• Just a score? The bot asks who it was against",
                "• Posted 1-1 mid-set? Post the final score after and it updates",
                "• Add the mode: mma or snb",
                "✅ The bot replies to confirm every result. No reply within a minute? It didn't go through, so post it again.",
                "",
                "📋 COMMANDS",
                "!board – full rankings, both modes",
                "!board mma – just MMA",
                "!board snb – just Stand & Bang",
                "!rank – your rank and record",
                "!rank bjkid – someone else's",
                "!record bjkid – your record vs BJkid2000",
                "!recent – last 5 results",
                "",
                f"❓ Problems or questions? Message {self.admin}",
            ]
            if self.site:
                lines.append(f"📊 Full stats: {self.site}")
            return ["\n".join(lines)]
        if cmd in ("board", "leaderboard", "top", "rankings"):
            # Everyone ranked, one message per mode so long lists stay readable.
            out = []
            for md in [mode] if mode else list(MODES):
                r = self.replay(md, now=now)
                rows = [f"{i}. {self.name(a)} {round(r['stats'][a]['rating'])}" for i, a in enumerate(r["board"], 1)]
                prov = [a for a, x in r["stats"].items() if x["provisional"]]
                text = f"🏆 {MODES[md].upper()}\n" + ("\n".join(rows) if rows else f"Nobody ranked yet ({PROVISIONAL_FIGHTS} fights needed).")
                if prov:
                    text += f"\n+{len(prov)} still getting their first {PROVISIONAL_FIGHTS} fights"
                out.append(text)
            if self.site:
                out[-1] += f"\n📊 {self.site}"
            return out
        if cmd in ("rank", "me", "stats"):
            acct = msg["sender_id"]
            if rest:
                status, ids = self._resolve(" ".join(rest), self._name_pool())
                if status != "ok":
                    return [f"Couldn't tell who \"{' '.join(rest)}\" is."]
                acct = ids[0]
            lines = [self.name(acct)]
            for md in [mode] if mode else list(MODES):
                r = self.replay(md, now=now)
                x = r["stats"].get(acct)
                if not x:
                    lines.append(f"{MODES[md]}: no fights yet")
                    continue
                where = f"#{x['rank']}" if x["rank"] else f"provisional {x['fights']}/{PROVISIONAL_FIGHTS}"
                streak = f" · {'W' if x['streak'] > 0 else 'L'}{abs(x['streak'])} streak" if x["streak"] else ""
                lines.append(f"{MODES[md]}: {where} · {round(x['rating'])} · {x['wins']}-{x['losses']}{streak}")
            return ["\n".join(lines) + site]
        if cmd in ("record", "h2h", "vs", "versus"):
            if not rest:
                return ["Type !record and their name, e.g. !record bjkid"]
            pool = self._name_pool()
            people = []
            for word in rest[:2]:
                status, ids = self._resolve(word, pool, exclude=set(people))
                if status != "ok":
                    return [f"Couldn't tell who \"{word}\" is."]
                people.append(ids[0])
            a, b = (people if len(people) == 2 else [msg["sender_id"], people[0]])
            lines = [f"{self.name(a)} vs {self.name(b)}"]
            for md in [mode] if mode else list(MODES):
                aw = bw = 0
                for f in self.s["fights"]:
                    if f["status"] != "ok" or f["mode"] != md or {f["winner"], f["loser"]} != {a, b}:
                        continue
                    gw, gl = f.get("score") or (1, 0)
                    if f["winner"] == a:
                        aw += gw; bw += gl
                    else:
                        bw += gw; aw += gl
                lines.append(f"{MODES[md]}: {aw}-{bw}")
            return ["\n".join(lines)]
        if cmd in ("recent", "last", "results"):
            fights = [f for f in self.s["fights"] if f["status"] == "ok" and (not mode or f["mode"] == mode)][-5:]
            if not fights:
                return ["No fights yet."]
            return ["\n".join(f"#{f['id']} {MODES[f['mode']]}: {self._describe(f)}" for f in reversed(fights))]
        return []

    # ------------------------------------------------------------ snapshot for the website
    def snapshot(self, now: int) -> dict[str, Any]:
        modes = {}
        for md, label in MODES.items():
            r = self.replay(md, now=now)
            players = []
            for acct, x in r["stats"].items():
                players.append({
                    "id": acct, "name": self.name(acct), "rating": round(x["rating"]), "rd": round(x["rd"]),
                    "rank": x["rank"], "provisional": x["provisional"],
                    "fights": x["fights"], "wins": x["wins"], "losses": x["losses"],
                    "sets_won": x["sets_won"], "sets_lost": x["sets_lost"], "streak": x["streak"],
                    "best": round(x["best_rating"]), "last": x["last"],
                })
            players.sort(key=lambda p: (p["rank"] is None, p["rank"] or 0, -p["rating"]))
            modes[md] = {
                "label": label, "players": players,
                "changes": {str(k): [round(v["winner"][1] - v["winner"][0]), round(v["loser"][1] - v["loser"][0])]
                            for k, v in r["changes"].items()},
            }
        fights = [dict(f, winner_name=self.name(f["winner"]), loser_name=self.name(f["loser"]),
                       reporter_name=self.name(f["reported_by"])) for f in self.s["fights"]]
        review = [dict(r, sender_name=self.name(r["sender"]) if r.get("sender") else "") for r in self.s["review"]]
        pending = [{"sender_name": self.name(q["sender"]), "kind": q["kind"], "text": q["text"], "ts": q["ts"]} for q in self.s["pending"]]
        return {
            "generated": now, "modes": modes, "fights": fights[::-1], "review": review[::-1], "pending": pending,
            "players": {a: p["name"] for a, p in self.s["players"].items()},
            "members": [a for a, p in self.s["players"].items() if p.get("in_group", True)],
            "log": self.s["log"][-60:][::-1], "meta": self.s["meta"], "bot_name": self.s["bot"].get("online_id"),
            "admin": self.admin,
            "settings": {"provisional": PROVISIONAL_FIGHTS, "duplicate_minutes": DUPLICATE_MINUTES},
        }
