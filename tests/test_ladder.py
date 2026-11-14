import pytest

from ladder.engine import DAY, H, Ladder, new_state
from ladder.names import resolve
from ladder.parser import SELF, parse

PLAYERS = {"a1": "JonoTheGoat", "a2": "Sam_K", "a3": "Temp48213", "a4": "Samurai_77", "a5": "xX_Reaper_Xx"}


# ---------------------------------------------------------------- parser
@pytest.mark.parametrize("text,winner,loser,score,mode", [
    ("beat sam 2-1 mma", SELF, "sam", (2, 1), "mma"),
    ("lost to jonno stand and bang", "jonno", SELF, None, "sb"),
    ("Temp beat Jono 2 0 sb", "temp", "jono", (2, 0), "sb"),
    ("w vs sam mma ko", SELF, "sam", None, "mma"),
    ("got smashed by jono 2-1 in mma", "jono", SELF, (2, 1), "mma"),
    ("sam beat me s&b", "sam", SELF, None, "sb"),
    ("gg sam, lost 1-2 mma", "sam", SELF, (2, 1), "mma"),
    ("just beat temp48 in a close one 2-1 Stand n bang", SELF, "temp48", (2, 1), "sb"),
    ("i got subbed by temp mma", "temp", SELF, None, "mma"),
    ("beat bjkid 2-1 SnB", SELF, "bjkid", (2, 1), "sb"),
])
def test_parse_results(text, winner, loser, score, mode):
    r = parse(text)
    assert r is not None
    assert (r.winner, r.loser, r.score, r.mode) == (winner, loser, score, mode)


@pytest.mark.parametrize("text", ["I'd beat sam easily", "who wants a game?", "ggs", "rematch?",
                                  "anyone on tonight", "next time I'll beat jono", "!board"])
def test_parse_ignores_chat(text):
    assert parse(text) is None


# ---------------------------------------------------------------- names
@pytest.mark.parametrize("query,expected", [
    ("temp", "a3"), ("temp48", "a3"), ("tmep", "a3"), ("temp 48213", "a3"), ("jonno", "a1"), ("goat", "a1"),
    ("smaurai", "a4"), ("samu", "a4"), ("sam", "a2"), ("reaper", "a5"), ("reeper", "a5"),
])
def test_name_guessing(query, expected):
    assert resolve(query, PLAYERS) == ("ok", [expected])


def test_two_temps_uses_recent_opponent_then_asks():
    players = dict(PLAYERS, a6="Temp99071")
    assert resolve("temp48", players) == ("ok", ["a3"])
    assert resolve("temp990", players) == ("ok", ["a6"])
    assert resolve("temp", players, prefer=["a6"]) == ("ok", ["a6"])
    status, ids = resolve("temp", players)
    assert status == "ask" and set(ids) == {"a3", "a6"}


def test_unknown_name():
    assert resolve("zzqx", PLAYERS)[0] == "none"


# ---------------------------------------------------------------- engine
T0 = 1_760_000_000_000


@pytest.fixture
def lad():
    s = new_state()
    s["bot"] = {"account_id": "bot", "online_id": "Bot"}
    l = Ladder(s, admin_name="BenE")
    l.sync_members([(a, n) for a, n in PLAYERS.items()] + [("bot", "Bot")])
    l._n = 0
    return l


def say(lad, who, body, t):
    lad._n += 1
    return lad.handle({"uid": f"m{lad._n}", "sender_id": who, "sender_name": PLAYERS.get(who, who), "body": body, "ts": t})


def test_result_logged_and_rated(lad):
    out = say(lad, "a1", "beat sam 2-1 mma", T0)
    assert out[0].startswith("✅ #1 MMA · JonoTheGoat def. Sam_K 2–1 · JonoTheGoat ")
    f = lad.s["fights"][0]
    assert (f["winner"], f["loser"], f["score"], f["mode"]) == ("a1", "a2", [2, 1], "mma")
    st = lad.replay("mma", now=T0)["stats"]
    assert st["a1"]["rating"] > 1500 > st["a2"]["rating"]
    assert st["a1"]["wins"] == 2 and st["a1"]["losses"] == 1


def test_both_players_posting_counts_once(lad):
    say(lad, "a1", "beat sam 2-1 mma", T0)
    out = say(lad, "a2", "lost to jono 2-1 mma", T0 + 5 * 60_000)
    assert "Already got that one" in out[0]
    assert len(lad.s["fights"]) == 1


def test_conflicting_reports_pause_for_admin(lad):
    say(lad, "a1", "beat sam 2-1 mma", T0)
    out = say(lad, "a2", "beat jono 2-0 mma", T0 + 5 * 60_000)
    assert "doesn't match" in out[0]
    assert lad.s["fights"][0]["status"] == "disputed"
    assert lad.s["review"][-1]["kind"] == "conflict"


def test_same_pair_other_mode_is_not_duplicate(lad):
    say(lad, "a1", "beat sam mma", T0)
    say(lad, "a2", "beat jono sb", T0 + H)
    assert len(lad.s["fights"]) == 2


def test_missing_mode_asks_then_answer(lad):
    out = say(lad, "a1", "beat sam 2-0", T0)
    assert "MMA or Stand & Bang" in out[0]
    out = say(lad, "a1", "mma", T0 + 60_000)
    assert out[0].startswith("✅ #1 MMA")


def test_missing_mode_guessed_from_same_session(lad):
    say(lad, "a1", "beat sam sb", T0)
    out = say(lad, "a2", "beat jono 2-1", T0 + 4 * H)  # different reporter, outside duplicate window
    assert out[0].startswith("✅ #2 Stand & Bang")


def test_ambiguous_name_asks_and_number_answers(lad):
    lad.sync_members([(a, n) for a, n in PLAYERS.items()] + [("a6", "Temp99071")])
    out = say(lad, "a1", "beat temp mma", T0)
    assert "Which one" in out[0]
    out = say(lad, "a1", "2", T0 + 60_000)
    assert out[0].startswith("✅")
    assert lad.s["fights"][0]["loser"] in {"a3", "a6"}


def test_banter_ignored(lad):
    for text in ["jono smashed it lol", "that beat was fire", "I'd beat sam easily", "ggs"]:
        assert say(lad, "a2", text, T0) == []
    assert lad.s["fights"] == [] and lad.s["pending"] == []


def test_unknown_name_flagged_then_cleared_by_repost(lad):
    out = say(lad, "a2", "beat zzqx 2-1 mma", T0)
    assert "Couldn't tell" in out[0]
    assert lad.s["review"][0]["kind"] == "unreadable"
    say(lad, "a2", "beat reaper 2-1 mma", T0 + H)
    assert lad.s["review"][0]["resolved"]


def test_unanswered_question_expires_to_review(lad):
    say(lad, "a1", "beat sam", T0)
    lad.expire_questions(T0 + 25 * H)
    assert lad.s["pending"] == [] and lad.s["review"][-1]["kind"] == "unanswered"


def test_board_needs_five_fights_and_never_drops_people(lad):
    t = T0
    for _ in range(2):
        say(lad, "a1", "beat sam 2-0 mma", t); t += 4 * H
    assert lad.replay("mma", now=t)["board"] == []          # 4 fights each: still provisional
    say(lad, "a1", "beat sam 2-0 mma", t)
    assert lad.replay("mma", now=t + 365 * DAY)["board"] == ["a1", "a2"]   # a year idle: still ranked


def test_one_line_reply(lad):
    out = say(lad, "a1", "beat sam 2-1 mma", T0)
    assert "\n" not in out[0] and "dispute" not in out[0]


def test_duplicate_only_within_ten_minutes(lad):
    say(lad, "a1", "beat sam mma", T0)
    assert "Already got" in say(lad, "a2", "lost to jono mma", T0 + 9 * 60_000)[0]
    say(lad, "a2", "lost to jono mma", T0 + 2 * H)          # a separate fight later on
    assert len(lad.s["fights"]) == 2


def test_commands(lad):
    say(lad, "a1", "beat sam 2-1 mma", T0)
    assert "Sam_K" in say(lad, "a2", "!rank", T0 + 1)[0]
    assert "MMA: 1-2" in say(lad, "a2", "!h2h jono", T0 + 2)[0]
    assert "MMA" in say(lad, "a2", "!board", T0 + 3)[0]
    assert "#1" in say(lad, "a2", "!recent", T0 + 5)[0]


def test_bot_ignores_itself_and_repeats(lad):
    assert lad.handle({"uid": "x", "sender_id": "bot", "sender_name": "Bot", "body": "beat sam mma", "ts": T0}) == []
    say(lad, "a1", "beat sam mma", T0)
    again = lad.handle({"uid": "m1", "sender_id": "a1", "sender_name": "JonoTheGoat", "body": "beat sam mma", "ts": T0})
    assert again == [] and len(lad.s["fights"]) == 1


def test_people_who_left_are_not_guessed(lad):
    lad.sync_members([(a, n) for a, n in PLAYERS.items() if a != "a4"] + [("bot", "Bot")])  # Samurai_77 left
    out = say(lad, "a1", "beat samu mma", T0)
    assert lad.s["fights"] == [] or lad.s["fights"][0]["loser"] != "a4"
