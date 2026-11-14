import pytest

from ladder.engine import DAY, H, Ladder, new_state
from ladder.names import resolve
from ladder.parser import SELF, parse

PLAYERS = {"a1": "JonoTheGoat", "a2": "Sam_K", "a3": "Temp48213", "a4": "Samurai_77", "a5": "xX_Reaper_Xx"}


# ---------------------------------------------------------------- parser
@pytest.mark.parametrize("text,winner,loser,score,mode", [
    ("beat sam 2-1 mma", SELF, "sam", (2, 1), "mma"),
    ("lost to jonno stand and bang", "jonno", SELF, None, "sb"),
    ("Temp beat Jono 2-0 sb", "temp", "jono", (2, 0), "sb"),
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
    l = Ladder(s, admin_name="BJkid2000")
    l.sync_members([(a, n) for a, n in PLAYERS.items()] + [("bot", "Bot")])
    l._n = 0
    return l


def say(lad, who, body, t):
    lad._n += 1
    return lad.handle({"uid": f"m{lad._n}", "sender_id": who, "sender_name": lad.s["players"].get(who, {}).get("name") or PLAYERS.get(who, who), "body": body, "ts": t})


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


def test_conflicting_reports_ask_which_is_right(lad):
    say(lad, "a1", "beat sam 2-1 mma", T0)
    out = say(lad, "a2", "beat jono 2-1 mma", T0 + 5 * 60_000)
    assert out[0] == ("JonoTheGoat and Sam_K, that doesn't look quite right. What was the result?\n"
                      "1) JonoTheGoat def. Sam_K 2–1\n2) Sam_K def. JonoTheGoat 2–1\n3) Other: needs BJkid2000 to review\n"
                      "Reply 1, 2 or 3\nNot a result? Reply nvm")
    assert lad.s["fights"][0]["status"] == "disputed"
    out = say(lad, "a1", "2", T0 + 6 * 60_000)              # jono (the other player) confirms Sam's version
    assert out[0].startswith("✅ #1 MMA sorted · Sam_K def. JonoTheGoat 2–1")
    assert lad.s["fights"][0]["status"] == "ok" and len(lad.s["fights"]) == 1


def test_same_pair_other_mode_is_not_duplicate(lad):
    say(lad, "a1", "beat sam 1-0 mma", T0)
    say(lad, "a2", "beat jono 1-0 sb", T0 + H)
    assert len(lad.s["fights"]) == 2


def test_missing_mode_asks_then_answer(lad):
    out = say(lad, "a1", "beat sam 2-0", T0)
    assert "MMA or Stand & Bang" in out[0]
    out = say(lad, "a1", "mma", T0 + 60_000)
    assert out[0].startswith("✅ #1 MMA")


def test_mode_is_never_guessed_from_earlier_fights(lad):
    say(lad, "a1", "beat sam 1-0 sb", T0)
    out = say(lad, "a2", "beat jono 2-1", T0 + 4 * H)       # same pair, no mode: still asks
    assert "MMA or Stand & Bang" in out[0]


def test_ambiguous_name_asks_and_number_answers(lad):
    lad.sync_members([(a, n) for a, n in PLAYERS.items()] + [("a6", "Temp99071")])
    out = say(lad, "a1", "beat temp 1-0 mma", T0)
    assert "did you mean?" in out[0]
    out = say(lad, "a1", "2", T0 + 60_000)
    assert out[0].startswith("✅")
    assert lad.s["fights"][0]["loser"] in {"a3", "a6"}


def test_banter_ignored(lad):
    for text in ["jono smashed it lol", "that beat was fire", "I'd beat sam easily", "ggs"]:
        assert say(lad, "a2", text, T0) == []
    assert lad.s["fights"] == [] and lad.s["pending"] == []


def test_unknown_name_asks_who(lad):
    out = say(lad, "a2", "beat zzqx 2-1 mma", T0)
    assert "couldn't find anyone called \"zzqx\" in this chat" in out[0]
    out = say(lad, "a2", "reaper", T0 + 60_000)
    assert out[0].startswith("✅ #1 MMA · Sam_K def. xX_Reaper_Xx 2–1")


def test_unanswered_question_expires_to_review(lad):
    say(lad, "a1", "beat sam 1-0", T0)
    lad.expire_questions(T0 + 4 * 60_000)
    assert lad.s["pending"] != []                           # still waiting at 4 minutes
    lad.expire_questions(T0 + 6 * 60_000)
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
    say(lad, "a1", "beat sam 1-0 mma", T0)
    assert "Already got" in say(lad, "a2", "lost to jono 0-1 mma", T0 + 9 * 60_000)[0]
    say(lad, "a2", "lost to jono 0-1 mma", T0 + 2 * H)          # a separate fight later on
    assert len(lad.s["fights"]) == 2


def test_commands(lad):
    say(lad, "a1", "beat sam 2-1 mma", T0)
    assert "Sam_K" in say(lad, "a2", "!rank", T0 + 1)[0]
    assert "MMA: 1-2" in say(lad, "a2", "!record jono", T0 + 2)[0]
    assert "MMA" in say(lad, "a2", "!board", T0 + 3)[0]
    assert "#1" in say(lad, "a2", "!recent", T0 + 5)[0]


def test_bot_ignores_itself_and_repeats(lad):
    assert lad.handle({"uid": "x", "sender_id": "bot", "sender_name": "Bot", "body": "beat sam 1-0 mma", "ts": T0}) == []
    say(lad, "a1", "beat sam 1-0 mma", T0)
    again = lad.handle({"uid": "m1", "sender_id": "a1", "sender_name": "JonoTheGoat", "body": "beat sam 1-0 mma", "ts": T0})
    assert again == [] and len(lad.s["fights"]) == 1


def test_people_who_left_are_not_guessed(lad):
    lad.sync_members([(a, n) for a, n in PLAYERS.items() if a != "a4"] + [("bot", "Bot")])  # Samurai_77 left
    out = say(lad, "a1", "beat samu 1-0 mma", T0)
    assert lad.s["fights"] == [] or lad.s["fights"][0]["loser"] != "a4"


def test_vs_lines_asks_mode_for_each_result(lad):
    lad.sync_members([(a, n) for a, n in PLAYERS.items()] + [("w", "mrwhoopdatahh"), ("m1", "Crazy_Maori6"), ("m2", "maoripunisher_")])
    out = say(lad, "w", "my fights today\nvs maori - 0-4\nvs sam - 1-0", T0)
    assert "did you mean?" in out[0] and "maori" in out[0]           # two Maoris: asks which
    out = say(lad, "w", "2", T0 + 60_000)
    assert out[0].startswith("Result 1 of 2: mrwhoopdatahh lost to maoripunisher_ 0-4\nMMA or Stand & Bang?\nReply mma or snb\nNot a result? Reply nvm")
    out = say(lad, "w", "snb", T0 + 120_000)
    assert out[0].startswith("Result 2 of 2: mrwhoopdatahh def. Sam_K 1-0\nMMA or Stand & Bang?")
    out = say(lad, "w", "mma", T0 + 180_000)
    assert out[0].count("✅") == 2
    f1, f2 = lad.s["fights"]
    assert (f1["winner"], f1["loser"], f1["score"], f1["mode"]) == ("m2", "w", [4, 0], "sb")
    assert (f2["winner"], f2["loser"], f2["score"], f2["mode"]) == ("w", "a2", [1, 0], "mma")


def test_vs_line_single_with_mode(lad):
    out = say(lad, "a1", "vs sam 2-1 mma", T0)
    assert out[0].startswith("✅ #1 MMA · JonoTheGoat def. Sam_K 2–1")
    out = say(lad, "a2", "vs jono 1-2 mma", T0 + 60_000)    # opponent posts the same thing
    assert "Already got" in out[0]


def test_split_counts_and_moves_ratings_toward_each_other(lad):
    t = T0
    for _ in range(2):                                        # jono gets ahead first
        say(lad, "a1", "beat sam 2-0 mma", t); t += 4 * H
    before = lad.replay("mma")["stats"]
    out = say(lad, "a2", "vs jono 2-2 mma", t)
    assert "split 2–2" in out[0]
    after = lad.replay("mma")["stats"]
    assert after["a2"]["rating"] > before["a2"]["rating"]   # lower-rated player gains
    assert after["a1"]["rating"] < before["a1"]["rating"]
    assert after["a1"]["wins"] == 6 and after["a1"]["losses"] == 2
    assert after["a1"]["streak"] == 2                        # a split doesn't break a streak
    out = say(lad, "a1", "vs sam 2-2 mma", t + 60_000)       # other player posts it too
    assert "Already got" in out[0]


def test_nickname_default_picks_without_asking():
    s = new_state()
    s["bot"] = {"account_id": "bot"}
    l = Ladder(s, nicknames={"maori": "maoripunisher_"})
    l.sync_members([("w", "mrwhoopdatahh"), ("m1", "Crazy_Maori6"), ("m2", "maoripunisher_")])
    out = l.handle({"uid": "1", "sender_id": "w", "sender_name": "mrwhoopdatahh", "body": "vs maori 0-4 snb", "ts": T0})
    assert out[0].startswith("✅ #1 Stand & Bang · maoripunisher_ def. mrwhoopdatahh 4–0")
    out = l.handle({"uid": "2", "sender_id": "w", "sender_name": "mrwhoopdatahh", "body": "beat crazy 1-0 mma", "ts": T0 + 1})
    assert "Crazy_Maori6" in out[0]                           # the other Maori is still reachable


def test_long_replies_are_split_under_the_limit():
    from ladder.run import MAX_MESSAGE, split_message
    s = new_state(); s["bot"] = {"account_id": "bot"}
    l = Ladder(s, admin_name="BJkid2000", site_url="https://ben-elsom.github.io/UFC4_Ladder/")
    l.sync_members(list(PLAYERS.items()))
    help_text = l.handle({"uid": "h", "sender_id": "a1", "sender_name": "x", "body": "!help", "ts": T0})[0]
    parts = split_message(help_text)
    assert len(parts) >= 2 and all(len(p) <= MAX_MESSAGE for p in parts)
    assert "".join(parts).replace("\n", "") == help_text.replace("\n", "")   # nothing lost
    board = "\n".join(f"{i}. player_number_{i} 1500" for i in range(1, 80))
    assert all(len(p) <= MAX_MESSAGE for p in split_message(board))
    assert split_message("short") == ["short"]


def test_set_posted_mid_way_then_finished_is_one_result(lad):
    say(lad, "a1", "vs sam 1-1 mma", T0)
    out = say(lad, "a1", "beat sam 2-1 mma", T0 + 25 * 60_000)        # the decider
    assert out[0].startswith("🔄 #1 MMA updated (was 1–1) · JonoTheGoat def. Sam_K 2–1")
    assert len(lad.s["fights"]) == 1
    st = lad.replay("mma")["stats"]
    assert (st["a1"]["wins"], st["a1"]["losses"]) == (2, 1)


def test_other_player_can_finish_the_set(lad):
    say(lad, "a1", "beat sam 1-0 mma", T0)                            # 1-0 after game one
    out = say(lad, "a2", "lost to jono 2-0", T0 + 20 * 60_000)        # no mode: asks, then updates
    assert "MMA or Stand & Bang" in out[0]
    out = say(lad, "a2", "mma", T0 + 21 * 60_000)
    assert "updated (was 1–0)" in out[0] and len(lad.s["fights"]) == 1
    assert lad.s["fights"][0]["score"] == [2, 0]


def test_finished_set_then_new_set_is_separate(lad):
    say(lad, "a1", "beat sam 2-1 mma", T0)
    say(lad, "a2", "beat jono 2-0 mma", T0 + 30 * 60_000)             # a new set, not a continuation
    say(lad, "a1", "beat sam 2-1 mma", T0 + 50 * 60_000)              # same score again, new set
    assert len(lad.s["fights"]) == 3


def test_update_window_closes_after_an_hour(lad):
    say(lad, "a1", "vs sam 1-1 mma", T0)
    say(lad, "a1", "beat sam 2-1 mma", T0 + 2 * H)
    assert len(lad.s["fights"]) == 2


def test_no_score_means_no_result(lad):
    for text in ["Wth im a easy snb win", "beat sam mma", "jono smashed it", "Last 1 wins", "see you at 5:30"]:
        assert say(lad, "a2", text, T0) == []
    assert lad.s["fights"] == [] and lad.s["pending"] == [] and lad.s["review"] == []


def test_bare_score_asks_who_then_later_score_updates_the_set(lad):
    out = say(lad, "a2", "1 -1", T0)                                   # sweatytrixter's mid-set post
    assert out[0].startswith("Sam_K, who was that 1-1 against?")
    out = say(lad, "a2", "reaper", T0 + 60_000)
    assert "MMA or Stand & Bang" in out[0]
    out = say(lad, "a2", "snb", T0 + 90_000)
    assert "split 1–1" in out[0]
    out = say(lad, "a2", "Safe as bruv 2-1 ", T0 + 20 * 60_000)       # the decider, no name
    assert out[0].startswith("🔄 #1 Stand & Bang updated (was 1–1) · Sam_K def. xX_Reaper_Xx 2–1")
    assert len(lad.s["fights"]) == 1


def test_bare_score_with_a_name_your_score_first(lad):
    out = say(lad, "a1", "sam 1-2 snb", T0)
    assert out[0].startswith("✅ #1 Stand & Bang · Sam_K def. JonoTheGoat 2–1")


def test_lost_word_beats_score_order(lad):
    say(lad, "a1", "lost 2-1", T0)                                     # asks who
    out = say(lad, "a1", "sam", T0 + 1000)
    out = say(lad, "a1", "mma", T0 + 2000)
    assert out[0].startswith("✅ #1 MMA · Sam_K def. JonoTheGoat 2–1")


def test_score_to_name_means_they_won(lad):
    lad.sync_members(list(PLAYERS.items()) + [("g", "grubby_flea257"), ("d", "dad_scopez")])
    say(lad, "g", "2-1 to scopes", T0)
    out = say(lad, "g", "snb", T0 + 1000)
    assert out[0].startswith("✅ #1 Stand & Bang · dad_scopez def. grubby_flea257 2–1")
    out = say(lad, "d", "2-1 me against grubby snb", T0 + 60_000)  # scopez posts the same: no conflict
    assert "Already got" in out[0]


def test_wordy_mode_answer_is_accepted(lad):
    say(lad, "a2", "2-1 me against jono", T0)
    out = say(lad, "a2", "Snb I won against jono and maori that's all I've played today I think", T0 + 60_000)
    assert out[0].startswith("✅ #1 Stand & Bang · Sam_K def. JonoTheGoat 2–1")


def test_unanswered_conflict_goes_to_review(lad):
    say(lad, "a1", "beat sam 2-1 mma", T0)
    say(lad, "a2", "beat jono 2-1 mma", T0 + 60_000)
    lad.expire_questions(T0 + 7 * 60_000)
    r = lad.s["review"][-1]
    assert r["kind"] == "unanswered" and r["fight_id"] == 1 and lad.s["fights"][0]["status"] == "disputed"


def test_conflict_other_goes_to_review(lad):
    say(lad, "a1", "beat sam 2-1 mma", T0)
    say(lad, "a2", "beat jono 2-0 mma", T0 + 60_000)
    out = say(lad, "a2", "3", T0 + 2 * 60_000)
    assert out[0].startswith("👍 Sent to")
    assert lad.s["pending"] == [] and lad.s["review"][-1]["fight_id"] == 1
    assert lad.s["fights"][0]["status"] == "disputed"         # doesn't count until the admin sorts it


@pytest.mark.parametrize("reply", ["nvm", "Nvm 😂", "never mind", "nevermind wasn't a result", "ignore that", "jk"])
def test_nvm_drops_question_without_review(lad, reply):
    out = say(lad, "a2", "Surely 2-0😆", T0)
    assert out[0].startswith("Sam_K, who was that 2-0 against?") and "Reply nvm" in out[0]
    out = say(lad, "a2", reply, T0 + 30_000)
    assert out == ["👍 No worries, ignored that one."]
    lad.expire_questions(T0 + 10 * 60_000)
    assert lad.s["pending"] == [] and lad.s["review"] == [] and lad.s["fights"] == []


def test_nvm_on_mode_question(lad):
    say(lad, "a1", "beat sam 1-0", T0)
    assert say(lad, "a1", "nvm", T0 + 10_000) == ["👍 No worries, ignored that one."]
    assert lad.s["fights"] == [] and lad.s["pending"] == []


def test_nvm_on_conflict_keeps_first_result(lad):
    say(lad, "a1", "beat sam 2-1 mma", T0)
    say(lad, "a2", "beat jono 2-1 mma", T0 + 60_000)
    out = say(lad, "a2", "nvm", T0 + 90_000)
    assert out[0].startswith("👍 No worries, ignored. #1 stays as JonoTheGoat def. Sam_K")
    assert lad.s["fights"][0]["status"] == "ok" and lad.s["review"] == []


def test_nvm_then_a_real_answer_still_works(lad):
    say(lad, "a2", "Surely 2-0", T0)
    assert say(lad, "a2", "nm just chilling", T0 + 10_000) == ["👍 No worries, ignored that one."]
    say(lad, "a2", "Surely 2-0", T0 + 20_000)
    assert say(lad, "a2", "jono", T0 + 30_000)[0].startswith("Sam_K def. JonoTheGoat 2-0\nMMA or Stand & Bang?\nReply mma or snb\nNot a result? Reply nvm")
