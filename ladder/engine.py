"""The ladder itself: reads chat messages, keeps the fight log, works out ratings.

Everything is rebuilt from the fight log each time, so fixing or removing a
fight is just changing that one entry.
"""

from __future__ import annotations

import copy
import random
import re
from datetime import date, datetime, time as dtime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import glicko
from .names import norm, resolve as _fuzzy_resolve
from .parser import SELF, VsLine, detect_mode, has_score, parse, parse_bare, parse_vs_lines

MODES = {"mma": "MMA", "sb": "Stand & Bang"}
PROVISIONAL_FIGHTS = 5     # fights before you appear on the board
COMBINED_WORDS = {"combined", "combo", "both", "all", "avg", "average", "overall"}
DUPLICATE_MINUTES = 10     # both players reporting the same result within this window = one result
SET_UPDATE_MINUTES = 60    # 1-1 then 2-1 within this long of the last post = the same set, updated
QUESTION_EXPIRES_MINUTES = 5   # a bot question not answered in this long goes to Needs review
MIN = 60 * 1000
H = 60 * MIN
DAY = 24 * H

# Daily points drop, so nobody can sit on a rank: everyone loses DECAY_POINTS at noon each day.
TIMEZONE = "Australia/Melbourne"
DECAY_POINTS = 10
DECAY_HOUR = 12
DECAY_FROM = "2026-11-18"     # first drop: noon on this day (earlier history is left alone)
BOARD_SIZE = 10               # !board shows this many per list; the site shows everyone


def noons(start_ms: int, end_ms: int) -> list[int]:
    """Daily drop times (ms) with start_ms < t <= end_ms."""
    tz = ZoneInfo(TIMEZONE)
    first = date.fromisoformat(DECAY_FROM)
    day = max(first, datetime.fromtimestamp(max(start_ms, 0) / 1000, tz).date())
    last = datetime.fromtimestamp(end_ms / 1000, tz).date()
    out = []
    while day <= last:
        t = int(datetime.combine(day, dtime(DECAY_HOUR), tz).timestamp() * 1000)
        if start_ms < t <= end_ms:
            out.append(t)
        day += timedelta(days=1)
    return out


def last_noon(now_ms: int) -> int | None:
    """The most recent drop time at or before now (None before the first one)."""
    ticks = noons(now_ms - 2 * DAY, now_ms)
    return ticks[-1] if ticks else None


def loses_points(x: dict) -> bool:
    """Who the daily drop applies to. Everyone with a rating in that mode."""
    return True


NVM_LINE = "- Not a result? Reply nvm"
SUB = "  - "   # second-level bullet

WEIGHT_CLASSES = [
    "Women's Strawweight", "Women's Flyweight", "Women's Bantamweight",
    "Men's Flyweight", "Men's Bantamweight", "Men's Featherweight", "Men's Lightweight",
    "Men's Welterweight", "Men's Middleweight", "Men's Light Heavyweight", "Men's Heavyweight",
]


PICK_GROUPS = {   # !pick <group>: what each word narrows the weight class to
    "Men's": [w for w in WEIGHT_CLASSES if w.startswith("Men's")],
    "Women's": [w for w in WEIGHT_CLASSES if w.startswith("Women's")],
    "Bigs": ["Men's Welterweight", "Men's Middleweight", "Men's Light Heavyweight", "Men's Heavyweight"],
    "Littles": ["Men's Flyweight", "Men's Bantamweight", "Men's Featherweight", "Men's Lightweight"],
}
PICK_WORDS = {"men": "Men's", "mens": "Men's", "male": "Men's", "women": "Women's", "womens": "Women's",
              "female": "Women's", "wmns": "Women's", "big": "Bigs", "bigs": "Bigs", "biggs": "Bigs",
              "little": "Littles", "littles": "Littles", "smalls": "Littles", "small": "Littles"}


def pick_group(words: list[str]) -> str | None:
    for w in words:
        g = PICK_WORDS.get(re.sub(r"[^a-z]", "", w.lower()))
        if g:
            return g
    return None


def pick_settings(rng=random, group: str | None = None) -> str:
    classes = PICK_GROUPS.get(group, WEIGHT_CLASSES)
    return "\n".join([
        f"🎲 Random fight settings{f' ({group})' if group else ''}",
        f"- Mode: {rng.choice(['MMA', 'Stand & Bang'])}",
        f"- Time: {rng.choice(['Accelerated', 'Real time'])}",
        f"- Rounds: {rng.choice([3, 5])}",
        f"- HUD: {rng.choice(['Shown', 'Hidden'])}",
        f"- Weight class: {rng.choice(classes)}",
    ])
NVM_RE = re.compile(r"^\W*(nvm|nvmd|nm|never\s*mind|nevermind|ignore( (it|that|me))?|forget (it|that)|cancel|not a result|wasn'?t a result|just (joking|kidding)|jk)\b", re.I)


def is_nevermind(body: str) -> bool:
    return len(body) <= 60 and bool(NVM_RE.match(body.strip()))


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

    def _resolve_opp(self, text: str, exclude: set[str] = frozenset(),
                     prefer: list[str] | None = None) -> tuple[str, list[str]]:
        """Who a new result is against. Matches everyone the bot has seen, so a name that
        means someone who left gives "left" rather than a guess at someone still here."""
        bot = self.s["bot"].get("account_id")
        everyone = {a: p["name"] for a, p in self.s["players"].items() if a != bot}
        status, ids = self._resolve(text, everyone, exclude=exclude, prefer=prefer)
        if status == "none":
            return status, ids
        here = [a for a in ids if self.s["players"][a].get("in_group", True)]
        if not here:
            return "left", ids[:1]
        return ("ok" if len(here) == 1 else "ask"), here

    def _left_msg(self, acct: str) -> str:
        return f"🚪 {self.name(acct)} has left the chat\n- Results against them can't be added"

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
    def replay(self, mode: str, now: int | None = None, cutoff: int | None = None) -> dict[str, Any]:
        """Ratings from the fight log. Daily drops are applied in time order up to `now`
        (or up to the last fight if no `now`). `cutoff`: ratings as they stood at that moment."""
        if cutoff is not None:
            now = cutoff
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

        clock = [None]   # time of the last fight or drop applied

        def drops_until(t: int) -> None:
            if clock[0] is not None:
                for _ in noons(clock[0], t):
                    for x in stats.values():
                        if loses_points(x):
                            x["rating"] -= DECAY_POINTS
            clock[0] = t if clock[0] is None else max(clock[0], t)

        for f in sorted(self.s["fights"], key=lambda f: f["ts"]):
            if f["mode"] != mode or f["status"] != "ok":
                continue
            if cutoff is not None and f["ts"] > cutoff:
                continue
            drops_until(f["ts"])
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

        if now is not None:
            drops_until(now)
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
        if "/" in body:
            return []  # a forward slash = "just talking, not a result", e.g. "I'm going to /3-0 you"

        pending = next((q for q in self.s["pending"]
                        if q["sender"] == msg["sender_id"] or msg["sender_id"] in q.get("players", ())), None)
        if pending and is_nevermind(body):
            return self._nevermind(pending)
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
            status, ids = self._resolve_opp(b.name, exclude={sender}, prefer=self._recent_opponents(sender))
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
        return [f"- {self.name(sender)}, who was that {b.mine}-{b.theirs} against?\n{NVM_LINE}"]

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
            status, ids = self._resolve_opp(text, exclude=exclude, prefer=prefer)
            if status == "left":
                return [self._left_msg(ids[0])] if strong else []
            if status == "ok":
                d[side] = ids[0]
            elif status == "ask":
                d["ask_side"], d["options"], d["other_text"] = side, ids, (l_text if side == "winner" else w_text)
                self._set_pending(d, "name")
                opts = "\n".join(f"{SUB}{i}) {self.name(a)}" for i, a in enumerate(ids, 1))
                return [f"- {self.name(sender)}, which \"{text}\" did you mean?\n{opts}\n- Reply with the number\n{NVM_LINE}"]
            else:
                if not strong:
                    return []  # probably just chat
                if sender in (d["winner"], d["loser"]) and d.get("score"):
                    hi, lo = d["score"]
                    mine, theirs = (hi, lo) if d["winner"] == sender else (lo, hi)
                    return self._vs_batch({"sender_id": sender, "uid": d["uid"], "ts": d["ts"]}, d["text"],
                                          [VsLine(name=text, mine=mine, theirs=theirs, mode=d["mode"], text=d["text"])])
                self._add_review("unreadable", f"Unknown player: \"{text}\"", d)
                return [f"❓ I couldn't find anyone in this chat called \"{text}\"\n"
                        f"- Post it again using part of their PSN name\n{SUB}e.g. beat bjkid 2-1 mma"]
        if d["winner"] == d["loser"]:
            return []
        return self._fill_mode(d)

    def _fill_mode(self, d: dict) -> list[str]:
        # Never guess the mode from earlier fights: always ask if it wasn't given.
        if d["mode"] is None:
            self._set_pending(d, "mode")
            sc = f" {d['score'][0]}-{d['score'][1]}" if d.get("score") else ""
            return [f"- {self.name(d['sender'])}, was that MMA or Stand & Bang?\n"
                    f"- {self.name(d['winner'])} def. {self.name(d['loser'])}{sc}\n{NVM_LINE}"]
        return self._finalise(d)

    def _set_pending(self, d: dict, kind: str) -> None:
        for old in [q for q in self.s["pending"] if q["sender"] == d["sender"]]:
            self.s["pending"].remove(old)
            self._add_review("unanswered", "No answer: posted another result instead", old)
        d["kind"] = kind
        d["asked_at"] = d.get("asked_at") or d["ts"]
        self.s["pending"].append(d)

    def _nevermind(self, q: dict) -> list[str]:
        """'nvm': they weren't posting a result, so drop the question (no Needs review)."""
        self.s["pending"].remove(q)
        if q["kind"] == "conflict":
            f = next((x for x in self.s["fights"] if x["id"] == q["fight_id"]), None)
            if f is not None:
                f["status"] = "ok"
                return [f"👍 No worries, ignored\n- Result stays as {self._describe(f)}"]
        return ["👍 No worries, ignored that one."]

    def _answer(self, q: dict, body: str, msg: dict) -> list[str] | None:
        words = body.lower().split()
        if q["kind"] == "batch":
            return self._answer_batch(q, body, words, msg["ts"])
        if q["kind"] == "conflict":
            return self._answer_conflict(q, body, words)
        if q["kind"] == "who":
            name = " ".join(w for w in words if w not in ("vs", "v", "against", "it", "was", "with"))
            if not name or len(words) > 4 or has_score(body):
                return None
            status, ids = self._resolve_opp(name, exclude={q["sender"]}, prefer=self._recent_opponents(q["sender"]))
            if status == "left":
                self.s["pending"].remove(q)
                return [self._left_msg(ids[0])]
            if status == "none":
                return [f"- {self.name(q['sender'])}, I couldn't find \"{name}\" in this chat\n{SUB}Reply with part of their PSN name\n{NVM_LINE}"]
            self.s["pending"].remove(q)
            return self._vs_batch(msg | {"uid": q["uid"], "ts": q["ts"]}, q["text"],
                                  [VsLine(name=self.name(ids[0]) if status == "ok" else name,
                                          mine=q["mine"], theirs=q["theirs"], mode=q["mode"], text=q["text"])])
        if q["kind"] == "mode":
            mode = detect_mode(body)
            if mode and not has_score(body):
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
        q["asked_at"] = msg["ts"]          # any follow-up question gets a fresh 5 minutes
        other_side = "loser" if q["ask_side"] == "winner" else "winner"
        if q[other_side] is None:
            w = SELF if q["winner"] == q["sender"] else (self.name(q["winner"]) if q["winner"] else q["other_text"])
            l = SELF if q["loser"] == q["sender"] else (self.name(q["loser"]) if q["loser"] else q["other_text"])
            return self._fill_names(q, w, l, True)
        return self._fill_mode(q)

    # ------------------------------------------------------------ "vs name score" messages
    def _vs_batch(self, msg: dict, body: str, lines: list) -> list[str]:
        """One message with one or more 'vs name a-b' lines. First number is the poster's."""
        sender = msg["sender_id"]
        prefer = self._recent_opponents(sender)
        whole_mode = detect_mode(body)
        drafts, notes = [], []
        for i, ln in enumerate(lines):
            if ln.mine == ln.theirs == 0:
                continue
            status, ids = self._resolve_opp(ln.name, exclude={sender}, prefer=prefer)
            if status == "left":
                notes.append(self._left_msg(ids[0]))
                continue
            drafts.append({
                "sender": sender, "uid": f"{msg['uid']}#{i}", "ts": msg["ts"], "text": ln.text,
                "won": ln.mine > ln.theirs, "score": (max(ln.mine, ln.theirs), min(ln.mine, ln.theirs)),
                "text_score": f"{ln.mine}-{ln.theirs}",
                "mode": ln.mode or whole_mode, "method": None,
                "opp": ids[0] if status == "ok" else None, "options": ids if status == "ask" else None,
                "unknown": status == "none",
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
                if d.get("unknown"):
                    return [f"- {self.name(q['sender'])}, I couldn't find anyone called \"{d['name_text']}\" in this chat\n"
                            f"{SUB}You posted: {d['text']}\n"
                            f"- Who did you play?\n{SUB}Reply with part of their PSN name, e.g. bjkid for BJkid2000\n{NVM_LINE}"]
                opts = "\n".join(f"{SUB}{i}) {self.name(a)}" for i, a in enumerate(d["options"], 1))
                return [f"- {self.name(q['sender'])}, which \"{d['name_text']}\" did you mean?\n{opts}\n- Reply with the number\n{NVM_LINE}"]
        missing = [d for d in q["drafts"] if d["mode"] is None]
        if missing:
            # Ask about each result in turn, never guess from earlier fights.
            d = missing[0]
            q["waiting"], q["asking"] = "mode", q["drafts"].index(d)
            q["asked_at"] = q.get("asked_at") or q["ts"]
            if new:
                self._set_pending(q, "batch")
            n, i = len(q["drafts"]), q["drafts"].index(d) + 1
            which = f" (result {i} of {n})" if n > 1 else ""
            hi, lo = d["score"]
            me, opp = self.name(q["sender"]), self.name(d["opp"])
            res = (f"{me} and {opp} split {hi}-{lo}" if hi == lo else
                   f"{me} def. {opp} {hi}-{lo}" if d["won"] else f"{me} lost to {opp} {lo}-{hi}")
            return [f"- {me}, was that MMA or Stand & Bang?\n- {res}{which}\n{NVM_LINE}"]
        if q in self.s["pending"]:
            self.s["pending"].remove(q)
        out = []
        for d in q["drafts"]:
            d["winner"], d["loser"] = (d["sender"], d["opp"]) if d["won"] else (d["opp"], d["sender"])
            out += self._finalise(d)
        return ["\n\n".join(out)] if out else []

    def _answer_conflict(self, q: dict, body: str, words: list[str]) -> list[str] | None:
        f = next((x for x in self.s["fights"] if x["id"] == q["fight_id"]), None)
        if f is None or len(words) > 3 or has_score(body):
            return None
        choice = words[0].strip(").") if words else ""
        if choice == "3" or (words and words[0] == "other"):
            self.s["pending"].remove(q)
            self._add_review("conflict", "Results don't match: players asked for admin",
                             q, fight_id=f["id"])
            return [f"👍 Sent to {self.admin} to check\n- The result won't count until it's sorted"]
        if choice not in ("1", "2"):
            # or they named the winner
            status, ids = self._resolve(body, {a: self.name(a) for a in q["players"]})
            if status != "ok":
                return None
            choice = "1" if ids[0] == f["winner"] else "2" if ids[0] == q["new"]["winner"] else ""
            if not choice:
                return None
        self.s["pending"].remove(q)
        if choice == "2":
            f["winner"], f["loser"], f["score"] = q["new"]["winner"], q["new"]["loser"], q["new"]["score"]
        f["status"] = "ok"
        return [self._confirm(f, f"✅ {MODES[f['mode']]} result sorted")]

    def _answer_batch(self, q: dict, body: str, words: list[str], now: int) -> list[str] | None:
        if q.get("waiting") == "name":
            d = q["drafts"][q["asking"]]
            pick = None
            if d.get("unknown"):
                if has_score(body) or len(words) > 3:
                    return None
                status, ids = self._resolve_opp(body, exclude={q["sender"]}, prefer=self._recent_opponents(q["sender"]))
                if status == "left":
                    return [f"{self._left_msg(ids[0])}\n- Reply with who else you played\n{NVM_LINE}"]
                if status == "none":
                    return [f"- {self.name(q['sender'])}, I still couldn't find \"{body.strip()}\" in this chat\n{SUB}Try a different part of their PSN name\n{NVM_LINE}"]
                if status == "ask":
                    d["unknown"], d["options"], d["name_text"] = False, ids, body.strip()
                    q["asked_at"] = now
                    return self._advance_batch(q)
                pick = ids[0]
            elif len(words) == 1 and words[0].strip(").") in {"1", "2", "3"}:
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
            q["asked_at"] = now
            return self._advance_batch(q)
        mode = detect_mode(body)
        if mode and not has_score(body):
            q["drafts"][q["asking"]]["mode"] = mode
            q["asked_at"] = now           # fresh 5 minutes for the next question
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
        return [self._confirm(f, f"🔄 {MODES[f['mode']]} result updated (was {was})")]

    def _session(self, d: dict) -> list[dict]:
        """This pair's results in this mode from the same sitting: newest first, each within
        SET_UPDATE_MINUTES of the next one (and the newest within it of this post)."""
        pair, out, last = {d["winner"], d["loser"]}, [], d["ts"]
        for f in reversed(self.s["fights"]):
            if f["status"] == "void" or f["mode"] != d["mode"] or {f["winner"], f["loser"]} != pair:
                continue
            if last - f.get("updated_ts", f["ts"]) > SET_UPDATE_MINUTES * MIN:
                break
            out.append(f)
            last = f["ts"]
        return out

    def _against_session(self, d: dict) -> list[str] | None:
        """A post that's the score for the whole sitting, not one more game.

        Applies when the other player posts, or when there's already more than one result
        tonight: e.g. you post 0-1 then 1-0, and they post 1-1 -> already got it, nothing added.
        A bigger total adds only the missing games. Anything else falls through to the usual checks."""
        session = self._session(d)
        if not session or (len(session) == 1 and session[0]["reported_by"] == d["sender"]):
            return None
        a, b = d["winner"], d["loser"]
        have = {a: sum(self._games(f, a) for f in session), b: sum(self._games(f, b) for f in session)}
        dw, dl = d["score"] or (1, 0)
        new = {a: dw, b: dl}
        me, opp = (a, b) if d["sender"] != b else (b, a)        # the poster's name first
        overall = f"{self.name(me)} {have[me]}–{have[opp]} {self.name(opp)} overall"
        if new == have:
            latest = session[0]
            if d["sender"] not in latest.get("confirmed_by", []) and d["sender"] != latest["reported_by"]:
                latest.setdefault("confirmed_by", []).append(d["sender"])
            latest["method"] = latest.get("method") or d["method"]
            self._resolve_unreadable(d)
            if len(session) == 1:
                return [f"👍 Already got that one\n- {self._describe(latest)}\n- Thanks for confirming"]
            return [f"👍 Already got that\n- {overall} ({MODES[d['mode']]})\n- Thanks for confirming"]
        if not (all(new[x] >= have[x] for x in (a, b)) and sum(new.values()) > sum(have.values())):
            return None
        if len(session) == 1:
            return self._update_set(session[0], d)
        extra = {x: new[x] - have[x] for x in (a, b)}
        w, l = (a, b) if extra[a] >= extra[b] else (b, a)
        add = dict(d, winner=w, loser=l, score=(extra[w], extra[l]))
        now = f"{self.name(me)} {new[me]}–{new[opp]} {self.name(opp)} overall"
        return self._record(add, f"✅ {MODES[d['mode']]} result added ({now})")

    def _finalise(self, d: dict) -> list[str]:
        whole = self._against_session(d)
        if whole is not None:
            return whole
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
                return [f"👍 Already got that one\n- {self._describe(f)}\n- Thanks for confirming"]
            f["status"] = "disputed"
            new = {"winner": d["winner"], "loser": d["loser"], "score": list(d["score"]) if d["score"] else None}
            q = {"sender": d["sender"], "players": [f["winner"], f["loser"]], "uid": d["uid"], "ts": d["ts"],
                 "text": d["text"], "fight_id": f["id"], "new": new}
            self._set_pending(q, "conflict")
            return [f"- {self.name(f['winner'])} and {self.name(f['loser'])}, that doesn't look quite right. What was the result?\n"
                    f"{SUB}1) {self._describe(f)}\n"
                    f"{SUB}2) {self._describe({**f, **new})}\n"
                    f"{SUB}3) Other: needs {self.admin} to review\n"
                    f"- Reply 1, 2 or 3\n{NVM_LINE}"]
        return self._record(d)

    def _record(self, d: dict, head: str | None = None) -> list[str]:
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
        return [self._confirm(fight, head or f"✅ {MODES[mode]} result")]

    def _confirm(self, fight: dict, head: str) -> str:
        after = self.replay(fight["mode"])
        ch = after["changes"][fight["id"]]

        def fmt(acct: str, pair: tuple[float, float]) -> str:
            delta = round(pair[1]) - round(pair[0])
            x = after["stats"][acct]
            tag = f" ({x['fights']}/{PROVISIONAL_FIGHTS} fights)" if x["provisional"] else ""
            return f"{self.name(acct)} {round(pair[1])} ({'+' if delta >= 0 else '−'}{abs(delta)}){tag}"

        return (f"{head}\n- {self._describe(fight)}\n"
                f"- {fmt(fight['winner'], ch['winner'])}\n- {fmt(fight['loser'], ch['loser'])}")

    def _describe(self, f: dict) -> str:
        sc = f.get("score")
        if sc and sc[0] == sc[1]:
            return f"{self.name(f['winner'])} and {self.name(f['loser'])} split {sc[0]}–{sc[1]}"
        s = f"{self.name(f['winner'])} def. {self.name(f['loser'])}"
        if f.get("score"):
            s += f" {f['score'][0]}–{f['score'][1]}"
        return s

    # ------------------------------------------------------------ disputes
    def dispute(self, msg: dict) -> list[str]:
        """Take the sender's most recent result off the ladder, the whole set, so it can be posted again.
        Only the most recent one: anything older goes to the admin."""
        who = msg["sender_id"]
        mine = [f for f in self.s["fights"] if f["status"] != "void" and who in (f["winner"], f["loser"])]
        if not mine:
            return [f"- {self.name(who)}, you don't have a result to dispute"]
        f = max(mine, key=lambda x: (x.get("updated_ts", x["ts"]), x["id"]))
        f["status"] = "void"
        f["note"] = f"disputed by {self.name(who)}"
        f.setdefault("disputes", []).append({"by": who, "ts": msg["ts"]})
        for q in [q for q in self.s["pending"] if q.get("fight_id") == f["id"]]:
            self.s["pending"].remove(q)
        for r in self.s["review"]:
            if not r["resolved"] and r.get("fight_id") == f["id"]:
                r["resolved"], r["resolution"] = True, f"disputed by {self.name(who)}"
        games = sum(f.get("score") or (1, 0))
        gone = {1: "That game is", 2: "Both games are"}.get(games, f"All {games} games are")
        return [f"⚠️ Result disputed by {self.name(who)}\n"
                f"- Removed: {self._describe(f)} ({MODES[f['mode']]})\n"
                f"- {gone} off the ladder and the points are reversed\n"
                f"- Post the correct result\n"
                f"{SUB}Older result wrong? Message {self.admin}"]

    def dispute_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.s["fights"]:
            for d in f.get("disputes", []):
                out[d["by"]] = out.get(d["by"], 0) + 1
        return out

    # ------------------------------------------------------------ review
    def _add_review(self, kind: str, reason: str, d: dict, fight_id: int | None = None) -> None:
        rid = self.s["next_review_id"]
        self.s["next_review_id"] += 1
        self.s["review"].append({
            "id": rid, "kind": kind, "reason": reason, "text": d.get("text", ""), "sender": d.get("sender"),
            "ts": d.get("ts"), "fight_id": fight_id, "resolved": False,
            "question": copy.deepcopy(d),     # what the bot knew, so the admin page can finish it off
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
            if now - (q.get("asked_at") or q["ts"]) > QUESTION_EXPIRES_MINUTES * MIN:
                self.s["pending"].remove(q)
                k = (q.get("waiting") or "name") if q["kind"] == "batch" else q["kind"]
                issue = {"mode": "No mode", "who": "No opponent", "name": "Unclear opponent",
                         "conflict": "Results don't match"}.get(k, "No answer")
                self._add_review("unanswered", issue, q,
                                 fight_id=q.get("fight_id"))

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

    def daily_update(self, tick: int, top: int = 3) -> str:
        """The noon post: everyone's drop, then the biggest gains and drops from fights since the last noon.
        Gains are measured after yesterday's drop and before today's, so the 10 points don't count against them."""
        prev = (noons(tick - 2 * DAY, tick - 1) or [tick - DAY])[-1]
        parts = [f"🕛 DAILY UPDATE\n- Everyone lost {DECAY_POINTS} points at 12pm\n{SUB}Fight to keep your spot"]
        for md, label in MODES.items():
            fought = {x for f in self.s["fights"] if f["status"] == "ok" and f["mode"] == md and prev < f["ts"] < tick
                      for x in (f["winner"], f["loser"])}
            if not fought:
                continue
            before = self.replay(md, cutoff=prev)["stats"]
            after = self.replay(md, cutoff=tick - 1)["stats"]      # just before today's drop
            moved = sorted(((round(after[a]["rating"]) - round(before[a]["rating"] if a in before else glicko.START_RATING), a)
                            for a in fought if a in after), key=lambda m: (-m[0], self.name(m[1]).lower()))
            up = [m for m in moved if m[0] > 0][:top]
            down = sorted((m for m in moved if m[0] < 0), key=lambda m: m[0])[:top]
            lines = [label.upper()]
            if up:
                lines += ["- Biggest gains"] + [f"{SUB}{self.name(a)} +{d}" for d, a in up]
            if down:
                lines += ["- Biggest drops"] + [f"{SUB}{self.name(a)} −{-d}" for d, a in down]
            parts.append("\n".join(lines))
        busy = self.fight_counts(lambda f: prev < f["ts"] < tick)
        if busy:
            parts.append("MOST FIGHTS TODAY\n" + "\n".join(f"- {i}. {self.name(a)} {n}" for i, (a, n) in enumerate(busy[:BOARD_SIZE], 1)))
        if len(parts) == 1:
            parts[0] += "\n- No fights in the last day"
        if self.site:
            parts[-1] += f"\n\n📊 {self.site}"
        return "\n\n".join(parts)

    def fight_counts(self, keep=lambda f: True) -> list[tuple[str, int]]:
        """Fights (every game in a set counts, both modes) per player, most first."""
        n: dict[str, int] = {}
        for f in self.s["fights"]:
            if f["status"] == "ok" and keep(f):
                games = sum(f.get("score") or (1, 0))
                for a in (f["winner"], f["loser"]):
                    n[a] = n.get(a, 0) + games
        return sorted(n.items(), key=lambda x: (-x[1], self.name(x[0]).lower()))

    def combined(self, now: int | None = None) -> list[dict]:
        """Players ranked (5+ fights) in both modes, best average rating first."""
        a, b = (self.replay(md, now=now)["stats"] for md in MODES)
        rows = [{"id": p, "mma": a[p]["rating"], "sb": b[p]["rating"], "avg": (a[p]["rating"] + b[p]["rating"]) / 2}
                for p in a if a[p]["rank"] and p in b and b[p]["rank"]]
        rows.sort(key=lambda r: (-round(r["avg"]), self.name(r["id"]).lower()))
        for i, r in enumerate(rows, 1):
            r["rank"] = i
        return rows

    def _command(self, msg: dict, parts: list[str]) -> list[str]:
        if not parts:
            return []
        cmd, args = parts[0], parts[1:]
        now = msg["ts"]
        both = any(a in COMBINED_WORDS for a in args)
        args = [a for a in args if a not in COMBINED_WORDS]
        mode, rest = self._mode_arg(args)
        site = f"\n\n📊 Full stats: {self.site}" if self.site else ""
        if cmd in ("help", "commands", "how"):
            lines = [
                "POST YOUR RESULTS TO THE GROUP CHAT AFTER YOUR FIGHT",
                '- Example: "vs bjkid 2-1 mma"  (your score first)',
                "- Winners are responsible for posting their results if they want them on the leaderboard",
                f"{SUB}No green tick, no points",
                f"- Everyone loses {DECAY_POINTS} points every day at 12pm, so keep fighting to hold your spot",
                "- Talking about a score, not posting one? Put a / in front, e.g. I'll /3-0 you",
            ]
            if self.site:
                lines.append(f"📖 How to use: {self.site.rstrip('/')}/#help")
            lines += [
                "",
                "📋 COMMANDS",
                "- !board – top 10: MMA, S&B and combined",
                "- !board mma – just MMA",
                "- !board snb – just Stand & Bang",
                "- !board combined – average of both modes",
                "- !rank – your rank and record, plus combined",
                "- !rank bjkid – someone else's",
                "- !record bjkid – your record vs BJkid2000",
                "- !record jono vs bjkid – record between any two players",
                "- !recent – last 5 results",
                "- !fights – who's had the most fights",
                "- !pick – random mode, time, rounds, HUD and weight class",
                "- !pick mens – men's divisions only",
                f"- !dispute – removes your most recent result (the whole set), then post it again. Older ones: message {self.admin}",
                "",
                f"❓ Problems or questions? Message {self.admin}",
            ]
            if self.site:
                lines.append(f"📊 Full stats: {self.site}")
            return ["\n".join(lines)]
        if cmd in ("fights", "active", "mostfights"):
            rows = self.fight_counts(lambda f: not mode or f["mode"] == mode)
            if not rows:
                return ["No fights yet."]
            title = f"🥊 MOST FIGHTS{' · ' + MODES[mode].upper() if mode else ''}"
            return [title + "\n" + "\n".join(f"- {i}. {self.name(a)} {n}" for i, (a, n) in enumerate(rows[:BOARD_SIZE], 1))
                    + (f"\n- +{len(rows) - BOARD_SIZE} more" if len(rows) > BOARD_SIZE else "") + site]
        if cmd == "dispute":
            return self.dispute(msg)
        if cmd in ("pick", "random", "roll"):
            return [pick_settings(group=pick_group(parts[1:]))]
        if cmd in ("board", "leaderboard", "top", "rankings"):
            # Top BOARD_SIZE, one message per mode so long lists stay readable. The site has everyone.
            def more(n: int) -> str:
                return f"\n- +{n} more ranked on the site" if n > 0 else ""

            out = []
            for md in [] if both else [mode] if mode else list(MODES):
                r = self.replay(md, now=now)
                rows = [f"- {i}. {self.name(a)} {round(r['stats'][a]['rating'])}" for i, a in enumerate(r["board"][:BOARD_SIZE], 1)]
                prov = [a for a, x in r["stats"].items() if x["provisional"]]
                text = f"🏆 {MODES[md].upper()}\n" + ("\n".join(rows) if rows else f"- Nobody ranked yet ({PROVISIONAL_FIGHTS} fights needed)")
                text += more(len(r["board"]) - BOARD_SIZE)
                if prov:
                    text += f"\n- +{len(prov)} still getting their first {PROVISIONAL_FIGHTS} fights"
                out.append(text)
            if both or not mode:
                comb = self.combined(now)
                rows = [f"- {r['rank']}. {self.name(r['id'])} {round(r['avg'])}" for r in comb[:BOARD_SIZE]]
                out.append("🏆 COMBINED (average of both)\n" + ("\n".join(rows) if rows else
                           f"- Nobody has {PROVISIONAL_FIGHTS} fights in both modes yet") + more(len(comb) - BOARD_SIZE))
            if self.site:
                out[-1] += f"\n\n📊 {self.site}"
            return out
        if cmd in ("rank", "me", "stats"):
            acct = msg["sender_id"]
            if rest:
                status, ids = self._resolve(" ".join(rest), self._name_pool())
                if status != "ok":
                    return [f"Couldn't tell who \"{' '.join(rest)}\" is."]
                acct = ids[0]
            lines = [self.name(acct)]
            for md in [] if both else [mode] if mode else list(MODES):
                r = self.replay(md, now=now)
                x = r["stats"].get(acct)
                if not x:
                    lines.append(f"- {MODES[md]}: no fights yet")
                    continue
                where = f"#{x['rank']}" if x["rank"] else f"provisional {x['fights']}/{PROVISIONAL_FIGHTS}"
                streak = f" · {'W' if x['streak'] > 0 else 'L'}{abs(x['streak'])} streak" if x["streak"] else ""
                lines.append(f"- {MODES[md]}: {where} · {round(x['rating'])} · {x['wins']}-{x['losses']}{streak}")
            if both or not mode:
                c = next((r for r in self.combined(now) if r["id"] == acct), None)
                lines.append(f"- Combined: #{c['rank']} · {round(c['avg'])}" if c else
                             f"- Combined: needs {PROVISIONAL_FIGHTS} fights in both modes")
            return ["\n".join(lines) + site]
        if cmd in ("record", "h2h", "vs", "versus"):
            if not rest:
                return ["Type !record and their name, e.g. !record bjkid"]
            people = self._record_people(rest)
            if isinstance(people, str):
                return [people]
            a, b = (people if len(people) == 2 else [msg["sender_id"], people[0]])
            if a == b:
                return ["Type two different players, e.g. !record jono vs bjkid"]
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
                lines.append(f"- {MODES[md]}: {aw}-{bw}")
            return ["\n".join(lines)]
        if cmd in ("recent", "last", "results"):
            fights = [f for f in self.s["fights"] if f["status"] == "ok" and (not mode or f["mode"] == mode)][-5:]
            if not fights:
                return ["No fights yet."]
            return ["🕒 Last results\n" + "\n".join(f"- {MODES[f['mode']]}: {self._describe(f)}" for f in reversed(fights))]
        return []

    def _record_people(self, words: list[str]) -> list[str] | str:
        """Who a !record is about: one name (vs the poster) or two. Names can be nicknames,
        including ones with spaces ("crazy maori")."""
        pool = self._name_pool()

        def one(text: str, exclude: set[str] = frozenset()) -> str | None:
            status, ids = self._resolve(text, pool, exclude=exclude)
            return ids[0] if status == "ok" else None

        parts = [p.strip() for p in re.split(r"\s+(?:vs?|versus|and|&)\s+|\s*,\s*", " ".join(words)) if p.strip()]
        if len(parts) == 2:
            a = one(parts[0])
            b = one(parts[1], {a} if a else set())
            for text, who in zip(parts, (a, b)):
                if not who:
                    return f"Couldn't tell who \"{text}\" is."
            return [a, b]
        text = " ".join(words)
        if norm(text) in self.nicknames or len(words) == 1:
            who = one(text)
            return [who] if who else f"Couldn't tell who \"{text}\" is."
        for i in range(1, len(words)):
            a = one(" ".join(words[:i]))
            b = one(" ".join(words[i:]), {a} if a else set())
            if a and b:
                return [a, b]
        who = one(text)
        return [who] if who else f"Couldn't tell who \"{text}\" is."

    # ------------------------------------------------------------ snapshot for the website
    def highlights(self, now: int) -> list[dict[str, Any]]:
        """Candidate cards for the top of the site's rankings page; the page shows three at random.
        Today = since midnight Melbourne time; week = the last 7 days. Fights that didn't count are skipped,
        and a card with nothing to show is left out."""
        tz = ZoneInfo(TIMEZONE)
        midnight = int(datetime.combine(datetime.fromtimestamp(now / 1000, tz).date(), dtime(0), tz).timestamp() * 1000)
        windows = {"today": (midnight, "today"), "week": (now - 7 * DAY, "this week")}
        ok = [f for f in self.s["fights"] if f["status"] == "ok"]
        changes = {md: self.replay(md)["changes"] for md in MODES}
        out: list[dict[str, Any]] = []

        def best(totals: dict[str, float]) -> tuple[str, float] | None:
            top = sorted(((a, v) for a, v in totals.items() if v > 0), key=lambda x: (-x[1], self.name(x[0]).lower()))
            return top[0] if top else None

        for key, (since, label) in windows.items():
            fights = [f for f in ok if since <= f["ts"] <= now]
            games: dict[str, int] = {}
            wins: dict[str, int] = {}
            points: dict[str, float] = {}
            for f in fights:
                gw, gl = f.get("score") or (1, 0)
                for a, won in ((f["winner"], gw), (f["loser"], gl)):
                    games[a] = games.get(a, 0) + gw + gl
                    wins[a] = wins.get(a, 0) + won
                ch = changes[f["mode"]].get(f["id"])
                if ch:
                    for side in ("winner", "loser"):
                        points[f[side]] = points.get(f[side], 0) + ch[side][1] - ch[side][0]
            total = sum(sum(f.get("score") or (1, 0)) for f in fights)
            if (b := best(games)):
                out.append({"key": f"fights_{key}", "title": f"Most fights {label}", "kind": "bar", "player": b[0],
                            "name": self.name(b[0]), "value": b[1], "unit": "fights", "of": total})
            if (b := best(wins)):
                out.append({"key": f"wins_{key}", "title": f"Most wins {label}", "kind": "player", "player": b[0],
                            "name": self.name(b[0]), "value": b[1], "unit": "wins" if b[1] != 1 else "win"})
            if (b := best(points)):
                out.append({"key": f"points_{key}", "title": f"Most points gained {label}", "kind": "player", "player": b[0],
                            "name": self.name(b[0]), "value": round(b[1]), "unit": "points", "signed": True})
        pairs: dict[tuple[str, str], list[int]] = {}
        for f in ok:
            a, b = sorted((f["winner"], f["loser"]))
            gw, gl = f.get("score") or (1, 0)
            r = pairs.setdefault((a, b), [0, 0])
            r[0 if f["winner"] == a else 1] += gw
            r[1 if f["winner"] == a else 0] += gl
        if pairs:
            (a, b), (wa, wb) = max(pairs.items(), key=lambda x: (sum(x[1]), self.name(x[0][0]).lower()))
            out.append({"key": "rivalry", "title": "Biggest rivalry", "kind": "pair", "players": [a, b],
                        "names": [self.name(a), self.name(b)], "value": wa + wb, "record": [wa, wb]})
        riser = None
        for md in MODES:
            cur, old = self.replay(md, now=now), self.replay(md, cutoff=now - DAY)
            for a in cur["board"]:
                if a in old["board"]:
                    mv = old["board"].index(a) - cur["board"].index(a)
                    if mv > 0 and (riser is None or mv > riser[1]):
                        riser = (a, mv, md, cur["stats"][a]["rating"])
        if riser:
            out.append({"key": "riser", "title": "Rising star", "kind": "player", "player": riser[0], "name": self.name(riser[0]),
                        "value": riser[1], "unit": f"place{'s' if riser[1] != 1 else ''} up in {MODES[riser[2]]}", "trend": True})
        if ok:
            out.append({"key": "logged", "title": "Fights logged", "kind": "number", "value": sum(sum(f.get("score") or (1, 0)) for f in ok)})
        return out

    def snapshot(self, now: int) -> dict[str, Any]:
        modes = {}
        for md, label in MODES.items():
            r = self.replay(md, now=now)
            old = self.replay(md, cutoff=now - DAY)["board"]
            form: dict[str, list[int]] = {}
            for f in sorted(self.s["fights"], key=lambda f: f["ts"]):
                ch = r["changes"].get(f["id"]) if f["mode"] == md and f["status"] == "ok" else None
                if ch:
                    for side in ("winner", "loser"):
                        form.setdefault(f[side], []).append(round(ch[side][1]))
            players = []
            for acct, x in r["stats"].items():
                players.append({
                    "id": acct, "name": self.name(acct), "rating": round(x["rating"]), "rd": round(x["rd"]),
                    "rank": x["rank"], "provisional": x["provisional"],
                    "fights": x["fights"], "wins": x["wins"], "losses": x["losses"],
                    "sets_won": x["sets_won"], "sets_lost": x["sets_lost"], "streak": x["streak"],
                    "best": round(x["best_rating"]), "last": x["last"],
                    "move": (old.index(acct) + 1 - x["rank"]) if x["rank"] and acct in old else None,
                    "new": bool(x["rank"]) and acct not in old,
                    "form": form.get(acct, [])[-12:],
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
        pending = [{"sender_name": self.name(q["sender"]), "kind": q["kind"], "waiting": q.get("waiting"), "text": q["text"],
                    "ts": q["ts"], "asked_at": q.get("asked_at") or q["ts"]} for q in self.s["pending"]]
        return {
            "generated": now, "modes": modes, "highlights": self.highlights(now),
            "combined": [{"id": r["id"], "rank": r["rank"], "avg": round(r["avg"])} for r in self.combined(now)], "fights": fights[::-1], "review": review[::-1], "pending": pending,
            "players": {a: p["name"] for a, p in self.s["players"].items()},
            "members": [a for a, p in self.s["players"].items() if p.get("in_group", True)],
            "log": self.s["log"][-60:][::-1], "meta": self.s["meta"], "bot_name": self.s["bot"].get("online_id"),
            "admin": self.admin,
            "settings": {"provisional": PROVISIONAL_FIGHTS, "duplicate_minutes": DUPLICATE_MINUTES,
                         "question_minutes": QUESTION_EXPIRES_MINUTES},
        }
