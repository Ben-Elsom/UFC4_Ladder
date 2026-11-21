"""Turn a chat message into a fight report, without needing an exact format.

Understands things like:
    beat sam 2-1 mma
    lost to jonno stand and bang
    Temp beat Jono 2 0 sb
    w vs sam mma ko
    got smashed by jono 2-1 in mma
    sam beat me
Returns None for normal chat ("I'd beat sam easily", "who wants a game?").
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SELF = "@self"  # placeholder for "the person who sent the message"

WIN_VERBS = {
    "beat", "beats", "bt", "def", "defeated", "smashed", "smoked", "destroyed", "dropped",
    "finished", "subbed", "bodied", "washed", "cooked", "battered", "whooped", "wooped",
    "clapped", "kod", "tkod", "stopped", "handled", "bested", "outpointed", "spanked",
    "slept", "murdered", "mauled", "embarrassed", "violated", "dusted", "pumped", "rinsed",
}
WON_VERBS = {"won", "win", "wins", "w", "dub"}
LOSS_VERBS = {"lost", "lose", "loses", "l", "loss", "fell", "dropped_to"}
SELF_WORDS = {"i", "me", "im", "ive", "myself", "i've", "i'm"}
FILLER = {
    "to", "vs", "v", "versus", "against", "over", "the", "a", "an", "in", "on", "by", "with",
    "and", "just", "got", "get", "gets", "was", "were", "been", "again", "game", "games",
    "set", "sets", "fight", "fights", "fought", "match", "bo3", "best", "of", "three", "3",
    "him", "her", "them", "bro", "mate", "lads", "boys", "gg", "ggs", "lol", "haha", "lmao",
    "easy", "close", "one", "tonight", "today", "yesterday", "finally", "again", "mode",
    "ranked", "round", "rd", "r", "it", "that", "this", "nice", "good", "great", "dude",
    "man", "all", "rounds", "up", "out", "off", "at", "for", "too", "so", "very", "really",
    "properly", "comfortably", "barely", "still", "yeah", "yep", "ok", "okay", "but", "then",
    "safe", "as", "bruv", "cuz", "cuh", "fam", "nah", "lol", "shi", "fr", "tbh", "ngl", "aye", "my",
    "final", "score", "result", "results", "updated", "update", "now", "ended", "end", "was", "is", "its", "it's",
    "wins", "win", "won", "loss", "lost", "series", "today", "tonight",
}
HYPOTHETICAL = re.compile(
    r"\b(would|i'?d|wouldn'?t|gonna|going to|will|wanna|want to|if|could|should|can'?t|cant|"
    r"bet|next|let'?s|lets|who|anyone|tryna|trying|might|maybe|never)\b"
)

MODE_PATTERNS = [
    ("sb", r"stand(?:ing)?\s*(?:and|an|n|&|\+|'n'|n')?\s*bang|s\s*&\s*b|s\s*\+\s*b|s\s*n\s*b|\bsnb\b|\bsb\b|\bs&b\b|\bstriking\b|\bbang\b"),
    ("mma", r"\bmma\b|\bm\.m\.a\b|\bfull\s*mma\b"),
]
METHOD_PATTERNS = [
    ("TKO", r"\btko'?d?\b|\bdoctor\s*stoppage\b|\bds\b"),
    ("KO", r"\bko'?d?\b|\bknock\s*out\b|\bknockout\b|\bkod\b|\bslept\b"),
    ("SUB", r"\bsub(?:bed|mission|mitted)?\b|\btap(?:ped)?\b|\bchoke(?:d)?\b"),
    ("DEC", r"\bdec(?:ision)?\b|\bpoints\b|\bsplit\b|\bunanimous\b|\bjudges\b"),
]
# A score like 2-1, 2 - 1, 2–1, 2:1 or 2/1. This is the key: no score, not a result.
SCORE_RE = re.compile(r"(?<![\d])([0-9])\s*(?:-|–|—|:|/)\s*([0-9])(?![\d])")
ROUND_RE = re.compile(r"\b(?:r|rd|round)\s*([1-5])\b")


@dataclass
class Report:
    winner: str          # raw name text, or SELF
    loser: str           # raw name text, or SELF
    score: tuple[int, int] | None = None   # (winner games, loser games)
    mode: str | None = None
    method: str | None = None
    winner_tokens: list[str] = field(default_factory=list)
    loser_tokens: list[str] = field(default_factory=list)
    implicit: bool = False   # opponent never named, only assumed to be the sender ("jono won")


def detect_mode(text: str) -> str | None:
    t = text.lower()
    found = {mode for mode, pat in MODE_PATTERNS if re.search(pat, t)}
    return found.pop() if len(found) == 1 else None


def _strip(pattern: str, text: str) -> str:
    return re.sub(pattern, " ", text)


def parse(text: str) -> Report | None:
    raw = text.strip()
    if not raw or raw.startswith("!") or raw.endswith("?"):
        return None
    t = raw.lower().replace("’", "'")
    if HYPOTHETICAL.search(t):
        return None

    t = re.sub(r"\b(t?ko)'d\b", r"\1d", t)
    mode = detect_mode(t)
    for _, pat in MODE_PATTERNS:
        t = _strip(pat, t)

    method = None
    for name, pat in METHOD_PATTERNS:
        if re.search(pat, t):
            method = method or name
            # "subbed"/"kod"/"slept" are also verbs; keep them for verb detection
            t = re.sub(pat, lambda m: m.group(0) if m.group(0) in WIN_VERBS else " ", t)

    t = ROUND_RE.sub(" ", t)
    score = None
    m = SCORE_RE.search(t)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if max(a, b) >= 1:   # 2-2 is a split: four fights, two each
            score = (max(a, b), min(a, b))
        t = t[: m.start()] + " " + t[m.end():]

    t = t.replace("got the dub", " won ").replace("took the w", " won ").replace("took the l", " lost ")
    # Only look at the clause with the result in it: "gg sam, lost 1-2" -> "lost"
    clauses = [c for c in re.split(r"[,;!\n]+|\.(?:\s|$)", t) if c.strip()]
    def has_verb(c: str) -> bool:
        toks = re.findall(r"[a-z0-9_'\-]+", c)
        return any((tok in WIN_VERBS or tok in WON_VERBS or tok in LOSS_VERBS) and (tok not in ("w", "l") or i == 0)
                   for i, tok in enumerate(toks))
    verb_clause = next((c for c in clauses if has_verb(c)), t)
    other_words = [w for c in clauses if c is not verb_clause for w in re.findall(r"[a-z0-9_\-]+", c)]
    t = verb_clause
    tokens = re.findall(r"[a-z0-9_'.\-]+", t)
    tokens = [tok.strip("'.-") for tok in tokens]
    tokens = [tok for tok in tokens if tok]

    # Find the verb. "w"/"l" only count as the first word ("w vs sam").
    idx, verb = None, None
    for i, tok in enumerate(tokens):
        if tok in ("w", "l") and i != 0:
            continue
        if tok in WIN_VERBS or tok in WON_VERBS or tok in LOSS_VERBS:
            idx, verb = i, tok
            break
    if idx is None:
        return None

    before, after = tokens[:idx], tokens[idx + 1:]
    # "got smashed by jono" / "sam got cooked" -> the subject lost
    passive = verb in WIN_VERBS and bool(before) and before[-1] in {"got", "get", "gets", "was", "been", "getting"}
    if passive and after[:1] == ["by"]:
        after = after[1:]

    def clean(side: list[str]) -> list[str]:
        return [tok for tok in side if tok not in FILLER and not tok.isdigit()]

    subj, obj = clean(before), clean(after)
    if not obj and (not subj or all(tok in SELF_WORDS for tok in subj)):
        # Opponent named in another part of the message: "gg sam, lost 1-2"
        obj = clean([w for w in other_words if w not in SELF_WORDS])[:2]
    subj_text = SELF if not subj or all(tok in SELF_WORDS for tok in subj) else " ".join(tok for tok in subj if tok not in SELF_WORDS)
    obj_has_self = any(tok in SELF_WORDS for tok in obj)
    obj = [tok for tok in obj if tok not in SELF_WORDS]
    obj_text = SELF if (obj_has_self and not obj) else " ".join(obj)

    # Too much going on = probably a conversation, not a report.
    if len(subj) > 3 or len(obj) > 4:
        return None

    if verb in LOSS_VERBS or passive:
        loser, winner = subj_text, obj_text or SELF
        if not obj_text:  # "sam lost" posted by jono -> jono beat sam
            winner = SELF
    else:
        winner, loser = subj_text, obj_text
        if not obj_text:  # "jono won 2-1" posted by sam -> jono beat sam
            loser = SELF
    if winner == loser:
        return None
    return Report(
        winner=winner, loser=loser, score=score, mode=mode, method=method,
        winner_tokens=winner.split() if winner != SELF else [],
        loser_tokens=loser.split() if loser != SELF else [],
        implicit=not obj_text,
    )


# ---------------------------------------------------------------- "vs name score" lines
# e.g.  my fights today
#       vs maori - 0-4
#       vs mika - 1-0 mma
# The first number is always the poster's score.
VS_LINE = re.compile(
    r"^\s*(?:vs\.?|v\.?|versus|against)\s+(?P<name>.+?)\s*[-:–—,]?\s*"
    r"(?P<a>\d{1,2})\s*(?:-|–|—|:|/|to)\s*(?P<b>\d{1,2})(?!\d)\s*(?P<rest>.*)$",
    re.IGNORECASE,
)


@dataclass
class VsLine:
    name: str
    mine: int
    theirs: int
    mode: str | None
    text: str
    games: list[bool] | None = None   # game by game, True = the poster won it (from a running tally)


def parse_tally(text: str) -> VsLine | None:
    """A set posted game by game, so the order is known:
        vs bjkid mma
        1-0
        1-1
        2-1
    = the poster won game 1, bjkid game 2, the poster game 3. Poster's score first. Each score must add
    exactly one game to the last, starting from 1-0 or 0-1, and the opponent must be named."""
    raw = text.strip()
    if not raw or raw.startswith("!") or raw.endswith("?"):
        return None
    t = raw.lower().replace("’", "'")
    if HYPOTHETICAL.search(t) or len(parse_vs_lines(raw)) > 1:
        return None
    t = ROUND_RE.sub(" ", t)
    scores = [(int(m.group(1)), int(m.group(2))) for m in SCORE_RE.finditer(t)]
    if len(scores) < 2:
        return None
    games, prev = [], (0, 0)
    for a, b in scores:
        step = (a - prev[0], b - prev[1])
        if step not in ((1, 0), (0, 1)):
            return None
        games.append(step == (1, 0))
        prev = (a, b)
    mode = detect_mode(t)
    rest = SCORE_RE.sub(" ", t)
    for _, pat in MODE_PATTERNS + METHOD_PATTERNS:
        rest = re.sub(pat, " ", rest)
    toks = [tok.strip("'.-") for tok in re.findall(r"[a-z0-9_'.\-]+", rest)]
    name = [tok for tok in toks if tok and not tok.isdigit() and tok not in FILLER and tok not in SELF_WORDS
            and tok not in WIN_VERBS and tok not in WON_VERBS and tok not in LOSS_VERBS]
    if not name or len(name) > 3:
        return None
    return VsLine(name=" ".join(name), mine=prev[0], theirs=prev[1], mode=mode, text=raw, games=games)


def parse_vs_lines(text: str) -> list[VsLine]:
    out: list[VsLine] = []
    for line in re.split(r"[\n;]+", text):
        m = VS_LINE.match(line.strip())
        if not m:
            continue
        name = m.group("name")
        for _, pat in MODE_PATTERNS:
            name = re.sub(pat, " ", name, flags=re.IGNORECASE)
        name = " ".join(tok for tok in re.findall(r"[A-Za-z0-9_\-]+", name) if tok.lower() not in FILLER)
        if not name:
            continue
        out.append(VsLine(name=name, mine=int(m.group("a")), theirs=int(m.group("b")),
                          mode=detect_mode(line), text=line.strip()))
    return out


# ---------------------------------------------------------------- the score is the key
def has_score(text: str) -> bool:
    t = ROUND_RE.sub(" ", text.lower())
    m = SCORE_RE.search(t)
    return bool(m) and max(int(m.group(1)), int(m.group(2))) >= 1


@dataclass
class Bare:
    mine: int            # the poster's games
    theirs: int
    mode: str | None
    name: str            # whatever is left that might be the opponent ("" if nothing)
    text: str


def parse_bare(text: str) -> Bare | None:
    """A score without a clear 'X beat Y': '2-1', 'safe as bruv 2-1', 'jono 2-1 snb', 'lost 1-2'.

    The poster's score goes first, unless they say won/lost, which decides it.
    """
    raw = text.strip()
    if not raw or raw.startswith("!") or raw.endswith("?"):
        return None
    t = raw.lower().replace("’", "'")
    if HYPOTHETICAL.search(t):
        return None
    t = ROUND_RE.sub(" ", t)
    m = SCORE_RE.search(t)
    if not m:
        return None
    a, b = int(m.group(1)), int(m.group(2))
    if max(a, b) < 1:
        return None
    mode = detect_mode(t)
    # "2-1 to scopes" = scopes won it; "2-1 to me" = I won it
    to_who = re.match(r"\s*(?:to|for)\s+([a-z0-9_\-]+)", t[m.end():])
    t = t[: m.start()] + " " + t[m.end():]
    for _, pat in MODE_PATTERNS + METHOD_PATTERNS:
        t = re.sub(pat, " ", t)
    toks = [tok.strip("'.-") for tok in re.findall(r"[a-z0-9_'.\-]+", t)]
    toks = [tok for tok in toks if tok and not tok.isdigit()]
    won = any(tok in WIN_VERBS or tok in WON_VERBS for tok in toks)
    lost = any(tok in LOSS_VERBS for tok in toks)
    if to_who and to_who.group(1) not in FILLER:
        won, lost = (True, False) if to_who.group(1) in SELF_WORDS else (False, True)
    if lost and not won:
        a, b = min(a, b), max(a, b)
    elif won and not lost:
        a, b = max(a, b), min(a, b)
    name = [tok for tok in toks if tok not in FILLER and tok not in SELF_WORDS
            and tok not in WIN_VERBS and tok not in WON_VERBS and tok not in LOSS_VERBS]
    return Bare(mine=a, theirs=b, mode=mode, name=" ".join(name[:3]), text=raw)
