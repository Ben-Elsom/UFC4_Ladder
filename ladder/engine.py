"""The ladder itself: reads chat messages, keeps the fight log, works out ratings and belts.

Everything is rebuilt from the fight log each time, so fixing or removing a
fight is just changing that one entry.
"""

from __future__ import annotations

import re
from typing import Any

from . import glicko
from .names import resolve
from .parser import SELF, detect_mode, parse

MODES = {"mma": "MMA", "sb": "Stand & Bang"}
PROVISIONAL_FIGHTS = 5     # fights before you appear on the board
INACTIVE_DAYS = 30         # off the board (and belt vacated) after this long idle
DUPLICATE_HOURS = 3        # both players reporting the same set within this window = one result
MODE_GUESS_HOURS = 6       # no mode given? use what the pair last played within this window
QUESTION_EXPIRES_HOURS = 24
BELT_MIN_RANKED = 3        # belt is first awarded once this many players are ranked
H = 3600 * 1000
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
    def __init__(self, state: dict[str, Any], admin_name: str = "the admin", site_url: str = ""):
        self.s = state
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
        belt: dict[str, Any] = {"holder": None, "since": None, "history": []}

        def st(acct: str) -> dict[str, Any]:
            return stats.setdefault(acct, {
                "rating": glicko.START_RATING, "rd": glicko.START_RD, "vol": glicko.START_VOL,
                "last": None, "fights": 0, "wins": 0, "losses": 0, "sets_won": 0, "sets_lost": 0,
                "streak": 0, "finishes": {"KO": 0, "TKO": 0, "SUB": 0, "DEC": 0}, "best_rating": glicko.START_RATING,
            })

        def ranked(at: int) -> list[str]:
            return sorted(
                (a for a, x in stats.items() if x["fights"] >= PROVISIONAL_FIGHTS and at - x["last"] <= INACTIVE_DAYS * DAY),
                key=lambda a: -stats[a]["rating"],
            )

        def belt_check(at: int, fight: dict | None) -> None:
            holder = belt["holder"]
            if holder and at - stats[holder]["last"] > INACTIVE_DAYS * DAY:
                belt["history"].append({"ts": at, "holder": None, "how": f"vacated ({self.name(holder)} inactive)"})
                belt["holder"] = holder = None
            if holder and fight and fight["loser"] == holder:
                belt["holder"], belt["since"] = fight["winner"], at
                belt["history"].append({"ts": at, "holder": fight["winner"], "how": f"beat {self.name(holder)}", "fight": fight["id"]})
                return
            if not holder:
                r = ranked(at)
                if len(r) >= BELT_MIN_RANKED:
                    belt["holder"], belt["since"] = r[0], at
                    belt["history"].append({"ts": at, "holder": r[0], "how": "crowned as #1"})

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
            w["sets_won"] += 1; l["sets_lost"] += 1
            w["streak"] = w["streak"] + 1 if w["streak"] > 0 else 1
            l["streak"] = l["streak"] - 1 if l["streak"] < 0 else -1
            if f.get("method") in w["finishes"]:
                w["finishes"][f["method"]] += 1
            w["last"] = l["last"] = f["ts"]
            w["best_rating"] = max(w["best_rating"], w["rating"])
            belt_check(f["ts"], f)

        if now is not None:
            belt_check(now, None)
        board = ranked(now) if now is not None else []
        for acct, x in stats.items():
            x["rank"] = board.index(acct) + 1 if acct in board else None
            x["provisional"] = x["fights"] < PROVISIONAL_FIGHTS
            x["inactive"] = now is not None and not x["provisional"] and now - x["last"] > INACTIVE_DAYS * DAY
        return {"stats": stats, "board": board, "changes": changes, "belt": belt}

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
        m = re.match(r"^\s*(dispute|wrong|undo|cancel|delete)\s*#?\s*(\d+)\b", low)
        if m:
            return self._dispute(msg, int(m.group(2)))

        pending = next((q for q in self.s["pending"] if q["sender"] == msg["sender_id"]), None)
        if pending:
            answer = self._answer(pending, body, msg)
            if answer is not None:
                return answer

        rep = parse(body)
        if rep is None:
            return []
        draft = {
            "sender": msg["sender_id"], "uid": msg["uid"], "ts": msg["ts"], "text": body,
            "winner": None, "loser": None, "score": rep.score, "mode": rep.mode, "method": rep.method,
        }
        strong = rep.score is not None or rep.mode is not None
        if rep.implicit and not strong:
            return []  # "jono smashed it" with no score or mode is just chat
        return self._fill_names(draft, rep.winner, rep.loser, strong)

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
            status, ids = resolve(text, pool, exclude=exclude, prefer=prefer)
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
            status, ids = resolve(body, {a: self.name(a) for a in q["options"]})
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

    def _finalise(self, d: dict) -> list[str]:
        pair = {d["winner"], d["loser"]}
        for f in reversed(self.s["fights"]):
            if f["status"] == "void" or f["mode"] != d["mode"] or {f["winner"], f["loser"]} != pair:
                continue
            if abs(d["ts"] - f["ts"]) > DUPLICATE_HOURS * H or f["reported_by"] == d["sender"]:
                continue
            same_winner = f["winner"] == d["winner"]
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
            return [f"⚠️ That doesn't match #{f['id']} ({self._describe(f)}). Both paused until {self.admin} sorts it."]
        return self._record(d)

    def _record(self, d: dict) -> list[str]:
        mode = d["mode"]
        before = self.replay(mode, now=d["ts"])
        fid = self.s["next_fight_id"]
        self.s["next_fight_id"] += 1
        fight = {
            "id": fid, "mode": mode, "winner": d["winner"], "loser": d["loser"],
            "score": list(d["score"]) if d["score"] else None, "method": d["method"],
            "reported_by": d["sender"], "ts": d["ts"], "msg_uid": d["uid"], "text": d["text"], "status": "ok",
        }
        self.s["fights"].append(fight)
        self._resolve_unreadable(d)
        after = self.replay(mode, now=d["ts"])
        ch = after["changes"][fid]
        wn, ln = self.name(d["winner"]), self.name(d["loser"])

        def fmt(acct: str, pair: tuple[float, float]) -> str:
            delta = round(pair[1]) - round(pair[0])
            x = after["stats"][acct]
            tag = f" (provisional {x['fights']}/{PROVISIONAL_FIGHTS})" if x["provisional"] else ""
            return f"{self.name(acct)} {round(pair[1])} ({'+' if delta >= 0 else '−'}{abs(delta)}){tag}"

        lines = [
            f"✅ #{fid} {MODES[mode]}: {self._describe(fight)}",
            f"{fmt(d['winner'], ch['winner'])} · {fmt(d['loser'], ch['loser'])}",
        ]
        if after["belt"]["holder"] != before["belt"]["holder"] and after["belt"]["holder"]:
            lines.append(f"🏆 {self.name(after['belt']['holder'])} is the {MODES[mode]} champ!")
        lines.append(f"Wrong? Reply: dispute {fid}")
        return ["\n".join(lines)]

    def _describe(self, f: dict) -> str:
        s = f"{self.name(f['winner'])} def. {self.name(f['loser'])}"
        if f.get("score"):
            s += f" {f['score'][0]}–{f['score'][1]}"
        if f.get("method"):
            s += f" ({f['method']})"
        return s

    # ------------------------------------------------------------ disputes & review
    def _dispute(self, msg: dict, fid: int) -> list[str]:
        f = next((x for x in self.s["fights"] if x["id"] == fid), None)
        if not f or f["status"] == "void":
            return [f"Couldn't find fight #{fid}."]
        who = msg["sender_id"]
        if who not in (f["winner"], f["loser"], f["reported_by"]):
            return [f"Only the two fighters can dispute #{fid}."]
        if who == f["reported_by"] and not f.get("confirmed_by"):
            f["status"] = "void"
            f["note"] = "removed by the person who posted it"
            return [f"🗑️ #{fid} removed. Post it again if it needs fixing."]
        if f["status"] == "disputed":
            return [f"#{fid} is already waiting for {self.admin} to check it."]
        f["status"] = "disputed"
        self._add_review("dispute", f"{self.name(who)} disputed it", {"text": msg["body"], "sender": who, "ts": msg["ts"], "uid": msg["uid"]}, fight_id=fid)
        return [f"⚠️ #{fid} paused (not counted) until {self.admin} checks it."]

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
            return [
                "🥊 Post results like:  beat sam 2-1 mma  ·  lost to temp sb\n"
                "Commands: !board  !board mma  !rank  !rank sam  !h2h sam  !belt  !recent\n"
                f"Wrong result? Reply: dispute <number>{site}"
            ]
        if cmd in ("board", "leaderboard", "top", "rankings"):
            modes = [mode] if mode else list(MODES)
            out = []
            for md in modes:
                r = self.replay(md, now=now)
                belt = r["belt"]["holder"]
                rows = [
                    f"{i}. {'🏆 ' if a == belt else ''}{self.name(a)} {round(r['stats'][a]['rating'])}"
                    for i, a in enumerate(r["board"][: 10 if mode else 5], 1)
                ]
                out.append(f"{MODES[md]}\n" + ("\n".join(rows) if rows else f"Nobody ranked yet ({PROVISIONAL_FIGHTS} fights needed)."))
            return ["\n\n".join(out) + site]
        if cmd in ("rank", "me", "stats"):
            acct = msg["sender_id"]
            if rest:
                status, ids = resolve(" ".join(rest), self._name_pool())
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
                where = (f"#{x['rank']}" if x["rank"] else
                         f"provisional {x['fights']}/{PROVISIONAL_FIGHTS}" if x["provisional"] else "inactive")
                streak = f" · {'W' if x['streak'] > 0 else 'L'}{abs(x['streak'])} streak" if x["streak"] else ""
                lines.append(f"{MODES[md]}: {where} · {round(x['rating'])} · {x['wins']}-{x['losses']}{streak}")
            return ["\n".join(lines) + site]
        if cmd in ("h2h", "vs", "record"):
            if not rest:
                return ["Usage: !h2h sam  (or !h2h sam jono)"]
            pool = self._name_pool()
            people = []
            for word in rest[:2]:
                status, ids = resolve(word, pool, exclude=set(people))
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
        if cmd in ("belt", "champ", "champs", "belts"):
            lines = []
            for md in MODES:
                h = self.replay(md, now=now)["belt"]["holder"]
                lines.append(f"🏆 {MODES[md]}: {self.name(h) if h else 'vacant'}")
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
                    "rank": x["rank"], "provisional": x["provisional"], "inactive": x["inactive"],
                    "fights": x["fights"], "wins": x["wins"], "losses": x["losses"],
                    "sets_won": x["sets_won"], "sets_lost": x["sets_lost"], "streak": x["streak"],
                    "finishes": x["finishes"], "best": round(x["best_rating"]), "last": x["last"],
                })
            players.sort(key=lambda p: (p["rank"] is None, p["rank"] or 0, -p["rating"]))
            modes[md] = {
                "label": label, "players": players,
                "belt": {"holder": r["belt"]["holder"], "holder_name": self.name(r["belt"]["holder"]) if r["belt"]["holder"] else None,
                         "since": r["belt"]["since"],
                         "history": [dict(h, holder_name=self.name(h["holder"]) if h["holder"] else None) for h in r["belt"]["history"]]},
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
            "log": self.s["log"][-60:][::-1], "meta": self.s["meta"], "bot_name": self.s["bot"].get("online_id"),
            "admin": self.admin,
            "settings": {"provisional": PROVISIONAL_FIGHTS, "inactive_days": INACTIVE_DAYS, "belt_min": BELT_MIN_RANKED},
        }
