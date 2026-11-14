"""Guess which player someone meant from a rough, misspelt or shortened name.

The goal is to almost never ask. "temp" finds Temp48213, "jonno" finds
JonoTheGoat, "smaurai" finds Samurai_77. Only when two players are genuinely
equally likely (two people both called Temp-something, and you typed "temp")
does it fall back to who you've played recently, and only then ask.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

ACCEPT = 0.60      # below this it isn't treated as a name at all
CLEAR_GAP = 0.06   # best guess must beat the runner-up by this much


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def digits(s: str) -> str:
    return "".join(ch for ch in s if ch.isdigit())


def core(name: str) -> str:
    """Name without leading/trailing numbers: Temp48213 -> temp."""
    n = norm(name)
    c = re.sub(r"^\d+|\d+$", "", n)
    return c or n


def parts(name: str) -> list[str]:
    """Split xX_Samurai_Xx / JonoTheGoat / Temp48213 into meaningful pieces."""
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    spaced = re.sub(r"([A-Za-z])(\d)|(\d)([A-Za-z])", r"\1\3 \2\4", spaced)
    bits = re.split(r"[^A-Za-z0-9]+", spaced)
    return [b.lower() for b in bits if len(b) >= 3 and not b.isdigit()]


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def score(query: str, name: str) -> float:
    q = norm(query)
    if len(q) < 2:
        return 0.0
    n, c = norm(name), core(name)
    if q == n:
        return 1.0
    if q == c:
        return 0.97
    best = 0.0
    qd, nd = digits(q), digits(name)
    q_letters = re.sub(r"\d", "", q)
    if qd and nd and len(q_letters) >= 3 and _ratio(q_letters, c) >= 0.8:
        # Letters plus some (maybe wrong) numbers: "temp48" / "temp4812" for Temp48213
        dig = 1.0 if nd.startswith(qd) else _ratio(qd, nd)
        return round(max(0.70 + 0.27 * dig * _ratio(q_letters, c), 0.95 * _ratio(q, n)), 4)
    # Start of the name: "jon" for JonoTheGoat
    if len(q) >= 3 and n.startswith(q):
        best = max(best, 0.80 + 0.15 * len(q) / len(n))
    # One piece of the name: "goat" for JonoTheGoat
    for p in parts(name):
        if q == p:
            best = max(best, 0.93)
        elif len(q) >= 3 and p.startswith(q):
            best = max(best, 0.85)
        elif len(p) >= 3 and q.startswith(p):
            best = max(best, 0.75)
        else:
            best = max(best, 0.92 * _ratio(q, p))
    # Plain misspelling of the whole thing or its letters
    best = max(best, 0.95 * _ratio(q, n), 0.95 * _ratio(q, c))
    return round(best, 4)


def resolve(query: str, players: dict[str, str], exclude: set[str] = frozenset(),
            prefer: list[str] | None = None) -> tuple[str, list[str]]:
    """Return ("ok", [id]), ("ask", [ids...]) or ("none", []).

    players: account_id -> online name. prefer: account ids ordered by how
    recently/often the reporter has played them, used to break near-ties.
    """
    scored = sorted(
        ((score(query, name), acct) for acct, name in players.items() if acct not in exclude),
        reverse=True,
    )
    if not scored or scored[0][0] < ACCEPT:
        return "none", []
    top = scored[0][0]
    close = [acct for s, acct in scored if top - s < CLEAR_GAP and s >= ACCEPT]
    if len(close) == 1:
        return "ok", close
    for acct in prefer or []:
        if acct in close:
            return "ok", [acct]
    return "ask", close[:3]
