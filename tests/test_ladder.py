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
    assert out[0].startswith("✅ MMA result\n- JonoTheGoat def. Sam_K 2–1\n- JonoTheGoat ")
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
    assert out[0] == ("- JonoTheGoat and Sam_K, that doesn't look quite right. What was the result?\n"
                      "  - 1) JonoTheGoat def. Sam_K 2–1\n  - 2) Sam_K def. JonoTheGoat 2–1\n  - 3) Other: needs BJkid2000 to review\n"
                      "- Reply 1, 2 or 3\n- Not a result? Reply nvm")
    assert lad.s["fights"][0]["status"] == "disputed"
    out = say(lad, "a1", "2", T0 + 6 * 60_000)              # jono (the other player) confirms Sam's version
    assert out[0].startswith("✅ MMA result sorted\n- Sam_K def. JonoTheGoat 2–1")
    assert lad.s["fights"][0]["status"] == "ok" and len(lad.s["fights"]) == 1


def test_same_pair_other_mode_is_not_duplicate(lad):
    say(lad, "a1", "beat sam 1-0 mma", T0)
    say(lad, "a2", "beat jono 1-0 sb", T0 + H)
    assert len(lad.s["fights"]) == 2


def test_missing_mode_asks_then_answer(lad):
    out = say(lad, "a1", "beat sam 2-0", T0)
    assert "MMA or Stand & Bang" in out[0]
    out = say(lad, "a1", "mma", T0 + 60_000)
    assert out[0].startswith("✅ MMA result")


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
    assert out[0].startswith("✅ MMA result\n- Sam_K def. xX_Reaper_Xx 2–1")


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
    assert out[0].count("\n") == 3 and "dispute" not in out[0] and "#" not in out[0]


def test_duplicate_only_within_ten_minutes(lad):
    say(lad, "a1", "beat sam 1-0 mma", T0)
    assert "Already got" in say(lad, "a2", "lost to jono 0-1 mma", T0 + 9 * 60_000)[0]
    say(lad, "a2", "lost to jono 0-1 mma", T0 + 2 * H)          # a separate fight later on
    assert len(lad.s["fights"]) == 2


def test_record_between_any_two_players_with_nicknames():
    s = new_state(); s["bot"] = {"account_id": "bot"}
    l = Ladder(s, admin_name="BJkid2000", nicknames={"crazy maori": "Crazy_Maori6", "maori": "maoripunisher_", "goat": "JonoTheGoat"})
    l.sync_members(list(PLAYERS.items()) + [("m1", "Crazy_Maori6"), ("m2", "maoripunisher_")])
    l._n = 0
    say(l, "a1", "beat sam 2-1 mma", T0)
    say(l, "m1", "beat maori 2-0 snb", T0 + H)
    for text in ["!record jono vs sam", "!record goat v sam_k", "!record jono and sam", "!record jono, sam", "!record goat sam"]:
        out = say(l, "a3", text, T0 + 2 * H)[0]                  # Temp asks about two other people
        assert out.startswith("JonoTheGoat vs Sam_K\n") and "- MMA: 2-1" in out, (text, out)
    out = say(l, "a3", "!record crazy maori vs maori", T0 + 2 * H)[0]
    assert out.startswith("Crazy_Maori6 vs maoripunisher_") and "Stand & Bang: 2-0" in out
    assert say(l, "m2", "!record crazy maori", T0 + 2 * H)[0].startswith("maoripunisher_ vs Crazy_Maori6")
    assert "Couldn't tell who \"zzqx\"" in say(l, "a3", "!record jono vs zzqx", T0 + 2 * H)[0]


def _games(lad):
    live = [f for f in lad.s["fights"] if f["status"] == "ok"]
    won = lambda who: sum((f.get("score") or (1, 0))[0 if f["winner"] == who else 1] for f in live if who in (f["winner"], f["loser"]))
    return won("a1"), won("a2")


def test_other_player_posting_the_session_score_is_not_counted_again(lad):
    say(lad, "a1", "0-1 vs sam snb", T0)                         # Sam won game 1
    say(lad, "a1", "vs sam 1-0 snb", T0 + 8 * 60_000)            # Jono won game 2
    out = say(lad, "a2", "1-1", T0 + 9 * 60_000)                 # Sam: "it's 1-1"
    assert out[0].startswith("👍 Already got that\n- Sam_K 1–1 JonoTheGoat overall (Stand & Bang)")
    assert _games(lad) == (1, 1) and len(lad.s["fights"]) == 2
    out = say(lad, "a2", "vs jono 1-2 snb", T0 + 20 * 60_000)    # later: "jono 2-1 now"
    assert out[0].startswith("✅ Stand & Bang result added (Sam_K 1–2 JonoTheGoat overall)\n- JonoTheGoat def. Sam_K 1–0")
    assert _games(lad) == (2, 1)


def test_running_total_after_single_games_adds_only_the_missing_ones(lad):
    say(lad, "a1", "beat sam 1-0 mma", T0)
    say(lad, "a1", "beat sam 1-0 mma", T0 + 10 * 60_000)         # per-game posting still counts
    assert _games(lad) == (2, 0)
    say(lad, "a1", "beat sam 3-0 mma", T0 + 20 * 60_000)         # then a running total
    assert _games(lad) == (3, 0)
    assert "Already got that" in say(lad, "a1", "beat sam 3-0 mma", T0 + 21 * 60_000)[0]
    assert _games(lad) == (3, 0)


def test_tally_update_and_other_player_confirm_still_work(lad):
    say(lad, "a1", "beat sam 1-0 mma", T0)
    assert "updated (was 1–0)" in say(lad, "a1", "beat sam 2-0 mma", T0 + 60_000)[0]
    assert "Already got that one" in say(lad, "a2", "lost to jono 0-2 mma", T0 + 30 * 60_000)[0]
    assert _games(lad) == (2, 0) and len(lad.s["fights"]) == 1


def test_forward_slash_means_not_a_result(lad):
    for text in ["I'm going to /0 and 1 you", "gonna /3-0 you sam", "/beat sam 2-1 mma"]:
        assert say(lad, "a1", text, T0) == []
    assert not lad.s["fights"] and not lad.s["pending"]
    assert say(lad, "a1", r"beat sam 2-1 mma \o/", T0) == []
    assert say(lad, "a1", "!rank", T0)                            # commands still work


def test_dispute_removes_your_most_recent_whole_set(lad):
    say(lad, "a1", "beat sam 1-0 snb", T0)
    say(lad, "a1", "beat sam 2-1 mma", T0 + H)
    out = say(lad, "a2", "!dispute", T0 + 2 * H)[0]               # Sam disputes: newest is the 2-1
    assert out == ("⚠️ Result disputed by Sam_K\n- Removed: JonoTheGoat def. Sam_K 2–1 (MMA)\n"
                   "- All 3 games are off the ladder and the points are reversed\n- Post the correct result\n"
                   "  - Older result wrong? Message BJkid2000")
    assert [f["status"] for f in lad.s["fights"]] == ["ok", "void"]
    assert "a1" not in lad.replay("mma")["stats"]
    say(lad, "a2", "lost to jono 2-0 mma", T0 + 3 * H)           # reposted
    assert say(lad, "a1", "dispute", T0 + 4 * H) == []                        # only the command counts
    assert "Both games are off" in say(lad, "a1", "!dispute", T0 + 4 * H)[0]  # Jono disputes that: fine
    assert "That game is off" in say(lad, "a1", "!Dispute", T0 + 5 * H)[0]    # then the 1-0 is his newest
    assert lad.dispute_counts() == {"a2": 1, "a1": 2}
    assert "don't have a result" in say(lad, "a1", "!dispute", T0 + 6 * H)[0]
    assert say(lad, "a3", "!undo", T0 + 6 * H) == []


def test_dispute_a_split_and_your_own_post(lad):
    say(lad, "a1", "vs sam 1-1 snb", T0)
    assert "Both games are off" in say(lad, "a1", "!dispute", T0 + 60_000)[0]
    assert all(f["status"] == "void" for f in lad.s["fights"])


MEL = __import__("zoneinfo").ZoneInfo("Australia/Melbourne")


def mel(y, mo, d, h=0, mi=0):
    from datetime import datetime
    return int(datetime(y, mo, d, h, mi, tzinfo=MEL).timestamp() * 1000)


def test_daily_drop_at_melbourne_noon(lad, monkeypatch):
    from ladder import engine
    monkeypatch.setattr(engine, "DECAY_FROM", "2026-11-18")
    say(lad, "a1", "beat sam 1-0 mma", mel(2026, 11, 16, 20))
    r0 = lad.replay("mma", now=mel(2026, 11, 18, 11, 59))["stats"]
    r1 = lad.replay("mma", now=mel(2026, 11, 18, 12, 0))["stats"]
    r3 = lad.replay("mma", now=mel(2026, 11, 20, 13))["stats"]
    for a in ("a1", "a2"):
        assert r1[a]["rating"] == pytest.approx(r0[a]["rating"] - 10)
        assert r3[a]["rating"] == pytest.approx(r0[a]["rating"] - 30)
    assert lad.replay("mma")["stats"]["a1"]["rating"] == pytest.approx(r0["a1"]["rating"])   # no `now`: as of the last fight
    # a later fight sees the dropped ratings first, so the drop carries through
    say(lad, "a2", "beat jono 1-0 mma", mel(2026, 11, 18, 13))
    r = lad.replay("mma", now=mel(2026, 11, 19, 12))
    assert r["changes"][2]["winner"][0] == pytest.approx(r1["a2"]["rating"])
    # both fought since the last noon, so neither drops at the next one
    assert r["stats"]["a2"]["rating"] == pytest.approx(r["changes"][2]["winner"][1])
    # day two's noon post: Sam's gain is just the fight, not the fight minus the drop
    post = lad.daily_update(mel(2026, 11, 19, 12))
    w = r["changes"][2]["winner"]
    assert f"  - Sam_K +{round(w[1]) - round(w[0])}\n" in post
    # history before the start date is untouched
    assert lad.replay("mma", cutoff=mel(2026, 11, 18, 11))["stats"]["a1"]["rating"] == pytest.approx(r0["a1"]["rating"])


def test_noon_follows_daylight_saving():
    from ladder import engine
    from datetime import datetime
    old = engine.DECAY_FROM
    engine.DECAY_FROM = "2027-04-01"
    try:
        ticks = engine.noons(mel(2027, 4, 2), mel(2027, 4, 6))   # clocks go back 4 Apr 2027
        assert [datetime.fromtimestamp(t / 1000, MEL).hour for t in ticks] == [12, 12, 12, 12]
        assert ticks[2] - ticks[1] == 25 * H
        assert engine.last_noon(mel(2027, 4, 5, 11)) == ticks[2] and engine.last_noon(mel(2027, 4, 5, 12)) == ticks[3]
    finally:
        engine.DECAY_FROM = old


def test_noon_post_once_with_biggest_movers(lad, monkeypatch):
    from ladder import engine
    from ladder.run import run
    monkeypatch.setattr(engine, "DECAY_FROM", "2026-11-18")

    class Chat:
        sent = []
        def identity(self): return "bot", "Bot"
        def group_label(self): return "g", "UFC 4: Elites"
        def members(self): return list(PLAYERS.items())
        def messages(self, limit=100): return []
        def send(self, text): self.sent.append(text)

    s = lad.s
    s["meta"]["started"] = s["meta"]["intro_sent"] = s["meta"]["last_weekly"] = mel(2026, 11, 18, 0)
    s["meta"]["last_daily"] = 0
    say(lad, "a1", "beat sam 2-0 mma", mel(2026, 11, 18, 9))
    say(lad, "a3", "beat jono 1-0 snb", mel(2026, 11, 18, 10))
    run(Chat(), s, {"dry_run": False}, mel(2026, 11, 18, 11, 59))
    assert Chat.sent == []                                         # not noon yet
    run(Chat(), s, {"dry_run": False}, mel(2026, 11, 18, 12, 1))
    run(Chat(), s, {"dry_run": False}, mel(2026, 11, 18, 12, 2))   # same noon: no repeat
    text = "\n".join(Chat.sent)
    assert len(Chat.sent) >= 1 and text.count("DAILY UPDATE") == 1
    assert text.startswith("🕛 DAILY UPDATE\n- Didn't fight since yesterday's 12pm? You lost 10 points\n  - Fight to keep your spot\n\nMMA\n- Biggest gains\n  - JonoTheGoat +")
    assert "- Biggest drops\n  - Sam_K −" in text and "STAND & BANG\n- Biggest gains\n  - Temp48213 +" in text
    assert "MOST FIGHTS TODAY\n- 1. JonoTheGoat 3\n- 2. Sam_K 2\n- 3. Temp48213 1" in text
    # gains are the fights only: the 10-point drop isn't taken off them
    jono_gain = int(text.split("  - JonoTheGoat +")[1].split("\n")[0])
    ch = lad.replay("mma")["changes"][1]["winner"]
    assert jono_gain == round(ch[1]) - round(ch[0])
    Chat.sent.clear()
    run(Chat(), s, {"dry_run": False}, mel(2026, 11, 21, 15))       # after days off: only the latest noon
    assert len(Chat.sent) == 1 and "No fights in the last day" in Chat.sent[0]


def test_board_shows_top_ten_only():
    s = new_state(); s["bot"] = {"account_id": "bot"}
    l = Ladder(s, admin_name="BJkid2000")
    people = {f"p{i}": f"Fighter{i:02d}" for i in range(14)}
    l.sync_members(list(people.items())); l._n = 0
    t = T0
    for i in range(14):                                  # everyone gets 5 fights against the next person
        for _ in range(3):
            t += H
            say(l, f"p{i}", f"beat fighter{(i + 1) % 14:02d} 1-0 mma", t)
    out = say(l, "p0", "!board mma", t + H)[0]
    assert out.count("\n- ") == 11 and "\n- 10. " in out and "\n- 11. " not in out and "- +4 more ranked on the site" in out


def test_pick_groups(lad):
    from ladder.engine import PICK_GROUPS
    for word, group in [("mens", "Men's"), ("men's", "Men's"), ("bigs", "Bigs"), ("littles", "Littles"), ("womens", "Women's")]:
        seen = set()
        for i in range(40):
            out = say(lad, "a1", f"!pick {word}", T0 + i)[0]
            assert out.startswith(f"🎲 Random fight settings ({group})")
            seen.add(out.split("- Weight class: ")[1])
        assert seen <= set(PICK_GROUPS[group]) and len(seen) > 1
    assert set(PICK_GROUPS["Bigs"]) | set(PICK_GROUPS["Littles"]) == set(PICK_GROUPS["Men's"])
    assert say(lad, "a1", "!pick", T0 + 99)[0].startswith("🎲 Random fight settings\n")


def test_fights_command(lad):
    say(lad, "a1", "beat sam 2-1 mma", T0)
    say(lad, "a3", "beat jono 1-0 snb", T0 + H)
    out = say(lad, "a2", "!fights", T0 + 2 * H)[0]
    assert out.startswith("🥊 MOST FIGHTS\n- 1. JonoTheGoat 4\n- 2. Sam_K 3\n- 3. Temp48213 1")
    assert say(lad, "a2", "!fights snb", T0 + 2 * H)[0].startswith("🥊 MOST FIGHTS · STAND & BANG\n- 1. JonoTheGoat 1\n- 2. Temp48213 1")


def test_site_highlight_cards(lad, monkeypatch):
    from ladder import engine
    monkeypatch.setattr(engine, "DECAY_FROM", "2026-11-18")
    say(lad, "a1", "beat sam 2-1 mma", mel(2026, 11, 11, 20))          # over a week ago
    say(lad, "a3", "vs sam 1-1 snb", mel(2026, 11, 19, 9))              # today: a split
    say(lad, "a2", "beat temp 2-0 snb", mel(2026, 11, 19, 10))
    cards = {c["key"]: c for c in lad.highlights(mel(2026, 11, 19, 15))}
    assert cards["fights_today"]["name"] == "Sam_K" and cards["fights_today"]["value"] == 4 and cards["fights_today"]["of"] == 4
    assert cards["wins_today"]["name"] == "Sam_K" and cards["wins_today"]["value"] == 3          # 1 from the split + 2
    assert cards["points_today"]["name"] == "Sam_K"
    ch = lad.replay("sb")["changes"]
    sam = sum(c[side][1] - c[side][0] for fid, c in ch.items() for side in ("winner", "loser")
              if next(f for f in lad.s["fights"] if f["id"] == fid)[side] == "a2")
    assert cards["points_today"]["value"] == round(sam)                  # fights only: the noon drop isn't counted
    assert cards["fights_week"]["value"] == 4 and "a1" not in (cards["fights_week"]["player"],)   # Jono's was 7+ days ago
    assert cards["rivalry"]["names"] == ["Sam_K", "Temp48213"] or cards["rivalry"]["value"] >= 3
    assert cards["logged"]["value"] == 7
    assert all(c["value"] > 0 for c in cards.values())
    assert lad.highlights(mel(2027, 1, 1)) and not [c for c in lad.highlights(mel(2027, 1, 1)) if c["key"].endswith(("today", "week"))]
    snap = lad.snapshot(mel(2026, 11, 19, 15))
    sam_sb = next(p for p in snap["modes"]["sb"]["players"] if p["id"] == "a2")
    assert len(sam_sb["form"]) == 2 and "move" in sam_sb and "new" in sam_sb and snap["highlights"]


def test_commands(lad):
    say(lad, "a1", "beat sam 2-1 mma", T0)
    assert "Sam_K" in say(lad, "a2", "!rank", T0 + 1)[0]
    assert "MMA: 1-2" in say(lad, "a2", "!record jono", T0 + 2)[0]
    assert "MMA" in say(lad, "a2", "!board", T0 + 3)[0]
    assert "- MMA: " in say(lad, "a2", "!recent", T0 + 5)[0]


def test_bot_ignores_itself_and_repeats(lad):
    assert lad.handle({"uid": "x", "sender_id": "bot", "sender_name": "Bot", "body": "beat sam 1-0 mma", "ts": T0}) == []
    say(lad, "a1", "beat sam 1-0 mma", T0)
    again = lad.handle({"uid": "m1", "sender_id": "a1", "sender_name": "JonoTheGoat", "body": "beat sam 1-0 mma", "ts": T0})
    assert again == [] and len(lad.s["fights"]) == 1


def test_people_who_left_are_not_guessed(lad):
    lad.sync_members([(a, n) for a, n in PLAYERS.items() if a != "a4"] + [("bot", "Bot")])  # Samurai_77 left
    out = say(lad, "a1", "beat samu 1-0 mma", T0)
    assert lad.s["fights"] == [] or lad.s["fights"][0]["loser"] != "a4"


def test_no_new_results_against_someone_who_left_even_with_past_fights(lad):
    say(lad, "a1", "beat samu 1-0 mma", T0)
    assert lad.s["fights"][0]["loser"] == "a4"
    lad.sync_members([(a, n) for a, n in PLAYERS.items() if a != "a4"] + [("bot", "Bot")])  # Samurai_77 left
    for i, text in enumerate(["vs samurai 1-0 snb", "beat samurai 2-1 mma", "Samurai_77 beat me 2-0 sb"]):
        say(lad, "a1", text, T0 + (i + 1) * DAY)
    assert len(lad.s["fights"]) == 1
    assert say(lad, "a1", "!rank samurai", T0 + 5 * DAY)[0].startswith("Samurai_77")  # still looked up for stats


def test_vs_lines_asks_mode_for_each_result(lad):
    lad.sync_members([(a, n) for a, n in PLAYERS.items()] + [("w", "mrwhoopdatahh"), ("m1", "Crazy_Maori6"), ("m2", "maoripunisher_")])
    out = say(lad, "w", "my fights today\nvs maori - 0-4\nvs sam - 1-0", T0)
    assert "did you mean?" in out[0] and "maori" in out[0]           # two Maoris: asks which
    out = say(lad, "w", "2", T0 + 60_000)
    assert out[0].startswith("- mrwhoopdatahh, was that MMA or Stand & Bang?\n- mrwhoopdatahh lost to maoripunisher_ 0-4 (result 1 of 2)\n- Not a result? Reply nvm")
    out = say(lad, "w", "snb", T0 + 120_000)
    assert out[0].startswith("- mrwhoopdatahh, was that MMA or Stand & Bang?\n- mrwhoopdatahh def. Sam_K 1-0 (result 2 of 2)")
    out = say(lad, "w", "mma", T0 + 180_000)
    assert out[0].count("✅") == 2
    f1, f2 = lad.s["fights"]
    assert (f1["winner"], f1["loser"], f1["score"], f1["mode"]) == ("m2", "w", [4, 0], "sb")
    assert (f2["winner"], f2["loser"], f2["score"], f2["mode"]) == ("w", "a2", [1, 0], "mma")


def test_vs_line_single_with_mode(lad):
    out = say(lad, "a1", "vs sam 2-1 mma", T0)
    assert out[0].startswith("✅ MMA result\n- JonoTheGoat def. Sam_K 2–1")
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
    assert after["a1"]["streak"] == 2                        # before the bonus, a split doesn't break a streak
    out = say(lad, "a1", "vs sam 2-2 mma", t + 60_000)       # other player posts it too
    assert "Already got" in out[0]


def test_nickname_default_picks_without_asking():
    s = new_state()
    s["bot"] = {"account_id": "bot"}
    l = Ladder(s, nicknames={"maori": "maoripunisher_"})
    l.sync_members([("w", "mrwhoopdatahh"), ("m1", "Crazy_Maori6"), ("m2", "maoripunisher_")])
    out = l.handle({"uid": "1", "sender_id": "w", "sender_name": "mrwhoopdatahh", "body": "vs maori 0-4 snb", "ts": T0})
    assert out[0].startswith("✅ Stand & Bang result\n- maoripunisher_ def. mrwhoopdatahh 4–0")
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
    assert out[0].startswith("🔄 MMA result updated (was 1–1)\n- JonoTheGoat def. Sam_K 2–1")
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
    assert out[0].startswith("- Sam_K, who was that 1-1 against?")
    out = say(lad, "a2", "reaper", T0 + 60_000)
    assert "MMA or Stand & Bang" in out[0]
    out = say(lad, "a2", "snb", T0 + 90_000)
    assert "split 1–1" in out[0]
    out = say(lad, "a2", "Safe as bruv 2-1 ", T0 + 20 * 60_000)       # the decider, no name
    assert out[0].startswith("🔄 Stand & Bang result updated (was 1–1)\n- Sam_K def. xX_Reaper_Xx 2–1")
    assert len(lad.s["fights"]) == 1


def test_bare_score_with_a_name_your_score_first(lad):
    out = say(lad, "a1", "sam 1-2 snb", T0)
    assert out[0].startswith("✅ Stand & Bang result\n- Sam_K def. JonoTheGoat 2–1")


def test_lost_word_beats_score_order(lad):
    say(lad, "a1", "lost 2-1", T0)                                     # asks who
    out = say(lad, "a1", "sam", T0 + 1000)
    out = say(lad, "a1", "mma", T0 + 2000)
    assert out[0].startswith("✅ MMA result\n- Sam_K def. JonoTheGoat 2–1")


def test_score_to_name_means_they_won(lad):
    lad.sync_members(list(PLAYERS.items()) + [("g", "grubby_flea257"), ("d", "dad_scopez")])
    say(lad, "g", "2-1 to scopes", T0)
    out = say(lad, "g", "snb", T0 + 1000)
    assert out[0].startswith("✅ Stand & Bang result\n- dad_scopez def. grubby_flea257 2–1")
    out = say(lad, "d", "2-1 me against grubby snb", T0 + 60_000)  # scopez posts the same: no conflict
    assert "Already got" in out[0]


def test_wordy_mode_answer_is_accepted(lad):
    say(lad, "a2", "2-1 me against jono", T0)
    out = say(lad, "a2", "Snb I won against jono and maori that's all I've played today I think", T0 + 60_000)
    assert out[0].startswith("✅ Stand & Bang result\n- Sam_K def. JonoTheGoat 2–1")


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
    assert out[0].startswith("- Sam_K, who was that 2-0 against?") and "Reply nvm" in out[0]
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
    assert out[0].startswith("👍 No worries, ignored\n- Result stays as JonoTheGoat def. Sam_K")
    assert lad.s["fights"][0]["status"] == "ok" and lad.s["review"] == []


def test_nvm_then_a_real_answer_still_works(lad):
    say(lad, "a2", "Surely 2-0", T0)
    assert say(lad, "a2", "nm just chilling", T0 + 10_000) == ["👍 No worries, ignored that one."]
    say(lad, "a2", "Surely 2-0", T0 + 20_000)
    assert say(lad, "a2", "jono", T0 + 30_000)[0].startswith("- Sam_K, was that MMA or Stand & Bang?\n- Sam_K def. JonoTheGoat 2-0\n- Not a result? Reply nvm")


def test_pick_gives_random_fight_settings():
    from ladder.engine import WEIGHT_CLASSES
    s = new_state(); s["bot"] = {"account_id": "bot"}
    l = Ladder(s, admin_name="BJkid2000")
    l.sync_members(list(PLAYERS.items()))
    seen = set()
    for i in range(60):
        out = l.handle({"uid": f"p{i}", "sender_id": "a1", "sender_name": "JonoTheGoat", "body": "!pick", "ts": T0 + i})
        assert len(out) == 1
        lines = out[0].split("\n")
        assert lines[1] in ("- Mode: MMA", "- Mode: Stand & Bang")
        assert lines[2] in ("- Time: Accelerated", "- Time: Real time")
        assert lines[3] in ("- Rounds: 3", "- Rounds: 5")
        assert lines[4] in ("- HUD: Shown", "- HUD: Hidden")
        assert lines[5].removeprefix("- Weight class: ") in WEIGHT_CLASSES
        seen.add(out[0])
    assert len(seen) > 1                       # actually random
    assert not s["fights"] and not s["pending"]   # no side effects


def test_combined_board_and_rank(lad):
    l, t = lad, T0
    for mode in ("mma", "snb"):
        for _ in range(5):                       # Jono and Sam: 5 fights each in both modes
            t += H
            say(l, "a1", f"beat sam 2-1 {mode}", t)
    for _ in range(5):                           # Temp only plays MMA
        t += H
        say(l, "a3", "beat reaper 2-0 mma", t)
    out = say(l, "a1", "!board", t + 1)
    assert len(out) == 3 and out[2].startswith("🏆 COMBINED")
    assert "1. JonoTheGoat" in out[2] and "2. Sam_K" in out[2] and "Temp" not in out[2]
    only = say(l, "a1", "!board combined", t + 2)
    assert len(only) == 1 and only[0] == out[2]
    assert "- Combined: #1 · " in say(l, "a1", "!rank", t + 3)[0]
    assert "- Combined: needs 5 fights in both modes" in say(l, "a3", "!rank", t + 4)[0]
    assert "Combined" not in say(l, "a1", "!board mma", t + 5)[0]
    snap = l.snapshot(t + 6)
    assert [c["id"] for c in snap["combined"]] == ["a1", "a2"]


def test_drop_skips_fighters_and_stops_at_floor(lad, monkeypatch):
    from ladder import engine
    monkeypatch.setattr(engine, "DECAY_FROM", "2026-11-18")
    say(lad, "a1", "beat sam 1-0 mma", mel(2026, 11, 17, 9))
    r0 = lad.replay("mma", now=mel(2026, 11, 17, 13))["stats"]
    monkeypatch.setattr(engine, "DECAY_FLOOR", r0["a2"]["rating"] - 15)
    say(lad, "a1", "beat jono 1-0 snb", mel(2026, 11, 18, 9))      # a fight in the other mode doesn't count
    r = lad.replay("mma", now=mel(2026, 11, 20, 13))["stats"]
    assert r["a1"]["rating"] == pytest.approx(r0["a1"]["rating"] - 30)
    assert r["a2"]["rating"] == pytest.approx(r0["a2"]["rating"] - 15)   # held at the floor


def test_game_streak_bonus_linear_uncapped_and_reset(lad):
    t = T0
    say(lad, "a1", "beat sam 1-0 mma", t - H)                       # before the bonus started: no bonus
    assert lad.replay("mma")["changes"][1]["bonus"] == 0
    lad.s["meta"]["streak_from"] = t
    out = say(lad, "a1", "beat sam 1-0 mma", t)
    ch = lad.replay("mma")["changes"][2]
    assert ch["bonus"] == 1 and ch["streak"] == 1                   # streaks start fresh: +1 for a 1-win streak
    assert "+1 streak bonus" in out[0]
    out = say(lad, "a1", "beat temp 3-0 mma", t + H)
    ch = lad.replay("mma")["changes"][3]
    assert ch["bonus"] == 2 + 3 + 4 and ch["streak"] == 4          # wins 2, 3 and 4 in a row
    assert "🔥4 win streak, +9 bonus" in out[0]
    say(lad, "a1", "beat sam 2-1 mma", t + 2 * H)                    # dropped a game: back to a 1-win streak
    st = lad.replay("mma")
    assert st["changes"][4]["bonus"] == 1 and st["stats"]["a1"]["streak"] == 1 and st["stats"]["a2"]["streak"] == 0
    say(lad, "a2", "vs jono 1-1 mma", t + 3 * H)                     # a split resets both
    st = lad.replay("mma")["stats"]
    assert st["a1"]["streak"] == 0 and st["a2"]["streak"] == 0


def test_new_player_starts_at_ranked_average(lad, monkeypatch):
    from ladder import engine
    monkeypatch.setattr(engine, "DECAY_FROM", "2026-11-18")
    t = mel(2026, 11, 17, 9)
    for i in range(3):
        say(lad, "a1", "beat sam 2-1 mma", t); t += H
    t = mel(2026, 11, 22, 9)                                          # four drops later
    lad.s["meta"]["streak_from"] = t
    r = lad.replay("mma", now=t)["stats"]
    avg = (r["a1"]["rating"] + r["a2"]["rating"]) / 2
    assert avg < 1480
    say(lad, "a3", "beat jono 1-0 mma", t)
    assert lad.replay("mma")["stats"]["a3"]["start"] == pytest.approx(avg)


def test_medals_and_days_at_number_one(lad):
    t = T0
    for i in range(3):
        say(lad, "a1", "beat sam 2-1 mma", t); t += H
    out = say(lad, "a1", "beat sam 1-0 mma", t)
    assert "- 👑 JonoTheGoat " in out[0] and "- 🥈 Sam_K " in out[0]
    assert "takes #1" not in out[0]                                   # already #1
    card = next(h for h in lad.highlights(t + 2 * DAY) if h["key"] == "reign_mma")
    assert card["name"] == "JonoTheGoat" and card["value"] == 3 and card["unit"] == "days at #1"
