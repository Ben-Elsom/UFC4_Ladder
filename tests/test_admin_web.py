import http.client
import re
import threading
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode

import pytest

from ladder import admin_ops as ops
from ladder import admin_web
from ladder.engine import Ladder, new_state

PLAYERS = {"a1": "BJkid2000", "a2": "Vboatt08", "a3": "mika_09876"}
T0 = 1_760_000_000_000


def make_state():
    s = new_state()
    s["bot"] = {"account_id": "bot"}
    lad = Ladder(s, admin_name="BJkid2000")
    lad.sync_members(list(PLAYERS.items()))
    for i, (sender, text) in enumerate([("a1", "beat vboat 2-1 mma"), ("a2", "beat mika 2-0 snb")]):
        lad.handle({"uid": str(i), "sender_id": sender, "sender_name": PLAYERS[sender], "body": text, "ts": T0 + i * 3_600_000})
    assert len(s["fights"]) == 2
    return s


# ---------------------------------------------------------------- ops
def test_void_and_restore():
    s = make_state()
    ops.void(s, 1)
    assert s["fights"][0]["status"] == "void"
    ops.restore(s, "1")
    assert s["fights"][0]["status"] == "ok"
    assert [e["action"] for e in s["fights"][0]["admin_edits"]] == ["removed", "restored"]


def test_edit_changes_everything_and_checks_input():
    s = make_state()
    ops.edit(s, 1, winner="a2", loser="a1", score=[2, 0], mode="sb")
    f = s["fights"][0]
    assert (f["winner"], f["loser"], f["score"], f["mode"]) == ("a2", "a1", [2, 0], "sb")
    for bad in [dict(winner="a1", loser="a1"), dict(score=[0, 0]), dict(score=[1, 2]), dict(mode="boxing"), dict(winner="zz")]:
        with pytest.raises(ops.AdminError):
            ops.edit(s, 1, **bad)
    ops.edit(s, 1, score=None)
    assert f["score"] is None
    with pytest.raises(ops.AdminError):
        ops.find_fight(s, 99)


def test_ratings_follow_an_edit():
    s = make_state()
    lad = Ladder(s)
    assert lad.replay("mma")["stats"]["a1"]["rating"] > 1500
    ops.edit(s, 1, winner="a2", loser="a1")
    assert lad.replay("mma")["stats"]["a1"]["rating"] < 1500


# ---------------------------------------------------------------- web
@pytest.fixture
def web():
    s = make_state()
    shared = admin_web.Shared(s)
    app = admin_web.AdminApp(shared, admin_web.hash_password("correct horse 99"), "x" * 64, secure_cookie=False)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), app.handler())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], s, app
    srv.shutdown()


def req(port, method, path, form=None, cookie=None, origin=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Content-Type": "application/x-www-form-urlencoded"}
    if cookie:
        h["Cookie"] = cookie
    if origin:
        h["Origin"] = origin
    c.request(method, path, body=urlencode(form or {}), headers=h)
    r = c.getresponse()
    return r.status, dict(r.getheaders()), r.read().decode()


def login(port):
    code, h, _ = req(port, "POST", "/admin/login", {"password": "correct horse 99"})
    assert code == 303 and h["Location"] == "/admin"
    return h["Set-Cookie"].split(";")[0]


def csrf_of(html):
    return re.search(r'name="csrf" value="([0-9a-f]+)"', html).group(1)


def test_everything_needs_a_login(web):
    port, s, _ = web
    for path in ["/admin", "/admin/fight/1", "/"]:
        code, h, body = req(port, "GET", path)
        assert code == 303 and h["Location"] == "/admin/login" and "BJkid2000" not in body
    code, h, _ = req(port, "POST", "/admin/fight/1/remove")
    assert code == 303 and h["Location"] == "/admin/login"
    assert s["fights"][0]["status"] == "ok"
    code, _, _ = req(port, "GET", "/admin", cookie="ladder_admin=9999999999.forged")
    assert code == 303


def test_wrong_passwords_lock_the_login(web):
    port, _, app = web
    app_sleep = admin_web.time.sleep
    admin_web.time.sleep = lambda _s: None
    try:
        for _ in range(admin_web.MAX_FAILS):
            assert req(port, "POST", "/admin/login", {"password": "nope"})[0] == 401
        code, _, body = req(port, "POST", "/admin/login", {"password": "correct horse 99"})
        assert code == 429 and "Too many" in body
    finally:
        admin_web.time.sleep = app_sleep


def test_list_remove_undo_and_edit(web):
    port, s, _ = web
    ck = login(port)
    code, _, body = req(port, "GET", "/admin", cookie=ck)
    assert code == 200 and "Vboatt08" in body and "mika_09876" in body
    tok = csrf_of(body)

    assert req(port, "POST", "/admin/fight/1/remove", {"csrf": "bad"}, ck)[0] == 403
    assert req(port, "POST", "/admin/fight/1/remove", {"csrf": tok}, ck, origin="https://evil.example")[0] == 403
    assert s["fights"][0]["status"] == "ok"

    code, h, _ = req(port, "POST", "/admin/fight/1/remove", {"csrf": tok}, ck)
    assert code == 303 and "ok=" in h["Location"] and s["fights"][0]["status"] == "void"
    req(port, "POST", "/admin/fight/1/restore", {"csrf": tok}, ck)
    assert s["fights"][0]["status"] == "ok"

    code, _, body = req(port, "GET", "/admin/fight/1", cookie=ck)
    assert code == 200 and "beat vboat 2-1 mma" in body
    code, _, body = req(port, "POST", "/admin/fight/1", {"csrf": tok, "winner": "a1", "loser": "a1", "ws": "2", "ls": "1", "mode": "mma"}, ck)
    assert code == 400 and "same person" in body
    code, _, _ = req(port, "POST", "/admin/fight/1", {"csrf": tok, "winner": "a2", "loser": "a1", "ws": "2", "ls": "0", "mode": "sb"}, ck)
    f = s["fights"][0]
    assert code == 303 and (f["winner"], f["loser"], f["score"], f["mode"]) == ("a2", "a1", [2, 0], "sb")
    assert "outbox" not in s                                    # plain Save posts nothing
    assert "Edit and Resend" in req(port, "GET", "/admin/fight/1", cookie=ck)[2]
    code, _, _ = req(port, "POST", "/admin/fight/1", {"csrf": tok, "winner": "a2", "loser": "a1", "ws": "2", "ls": "1",
                                                     "mode": "sb", "resend": "1"}, ck)
    assert code == 303 and f["score"] == [2, 1]
    msg = s["outbox"].pop()
    assert msg.startswith(f"🎙️ WE HAVE A CORRECTION\n- Stand & Bang result, was: {PLAYERS['a2']} def. {PLAYERS['a1']} 2–0\n"
                          f"- {PLAYERS['a2']} def. {PLAYERS['a1']} 2–1\n- {PLAYERS['a2']} ")
    assert msg.count("\n") == 4
    code, _, _ = req(port, "POST", "/admin/fight/2/resend", {"csrf": tok}, ck)
    assert code == 303 and s["outbox"].pop().startswith("🔁 Stand & Bang result (resent)\n- ")
    ops.void(s, 2)
    code, h, _ = req(port, "POST", "/admin/fight/2/resend", {"csrf": tok}, ck)
    assert code == 303 and "err=" in h["Location"] and not s["outbox"]


def test_admin_page_shows_disputes(web):
    port, s, _ = web
    lad = Ladder(s, admin_name="BJkid2000")
    lad.dispute({"sender_id": "a2", "ts": T0 + 99_000_000})
    body = req(port, "GET", "/admin", cookie=login(port))[2]
    assert "<h2>Disputes</h2>" in body and f"Disputed by {PLAYERS['a2']}" in body and "Undo remove" in body


def test_flag_shows_five_earlier_messages(web):
    port, s, _ = web
    s["chat_log"] = [{"uid": f"c{i}", "ts": T0 + 8_000_000 + i, "sender_id": "a1", "sender_name": "x", "body": f"line {i}"} for i in range(8)]
    flag(s, "a2", "beat bjkid 2-1", T0 + 9_000_000)
    body = req(port, "GET", "/admin", cookie=login(port))[2]
    assert "Earlier messages" in body and "line 3" in body and "line 7" in body and "line 2" not in body


def test_chat_text_never_goes_in_the_public_state_file(tmp_path):
    import json
    from ladder.run import load_state, remember_chat, save_state
    s = make_state()
    remember_chat(s, [{"uid": "u1", "ts": 1, "sender_id": "a1", "sender_name": "x", "body": "secret chat"}])
    save_state(tmp_path / "state.json", s)
    assert "secret chat" not in (tmp_path / "state.json").read_text()
    assert json.loads((tmp_path / "chat_log.json").read_text())[0]["body"] == "secret chat"
    assert load_state(tmp_path / "state.json")["chat_log"][0]["body"] == "secret chat"


def test_html_in_names_is_escaped(web):
    port, s, _ = web
    s["players"]["a3"]["name"] = "<script>x</script>"
    body = req(port, "GET", "/admin", cookie=login(port))[2]
    assert "<script>x</script>" not in body and "&lt;script&gt;" in body


def test_password_hash_round_trip():
    h = admin_web.hash_password("abc def ghi")
    assert admin_web.check_password("abc def ghi", h) and not admin_web.check_password("abc def gh", h)
    assert not admin_web.check_password("x", "garbage")


# ---------------------------------------------------------------- flagged messages
def flag(s, sender, text, ts):
    lad = Ladder(s, admin_name="BJkid2000")
    out = lad.handle({"uid": f"f{ts}", "sender_id": sender, "sender_name": PLAYERS[sender], "body": text, "ts": ts})
    lad.expire_questions(ts + 10 * 60_000)
    return out, s["review"][-1]


def test_flag_keeps_what_the_bot_knew_and_admin_can_finish_it():
    s = make_state()
    _, r = flag(s, "a2", "beat bjkid 2-1", T0 + 9_000_000)
    assert r["reason"] == "No mode" and r["question"]["winner"] == "a2"
    rows = ops.guess_rows(s, r, Ladder)
    assert rows == [dict(winner="a2", loser="a1", score=[2, 1], mode=None, text="beat bjkid 2-1", options=[])]
    msg = admin_web.add_from_form(s, r["id"], {"rows": "1", "w0": "a2", "l0": "a1", "ws0": "2", "ls0": "1", "m0": "mma"})
    f = s["fights"][-1]
    assert msg == f"Added result {f['id']}" and (f["winner"], f["loser"], f["mode"], f["score"]) == ("a2", "a1", "mma", [2, 1])
    assert f["ts"] == T0 + 9_000_000 and r["resolved"]
    with pytest.raises(ops.AdminError):
        admin_web.add_from_form(s, r["id"], {"rows": "1", "w0": "a2", "l0": "a1", "m0": "mma"})   # already sorted


def test_missing_opponent_row_and_validation():
    s = make_state()
    _, r = flag(s, "a3", "2-0", T0 + 9_000_000)
    assert r["reason"] == "No opponent"
    assert ops.guess_rows(s, r, Ladder)[0] == dict(winner="a3", loser=None, score=[2, 0], mode=None, text="2-0", options=[])
    with pytest.raises(ops.AdminError, match="both players"):
        admin_web.add_from_form(s, r["id"], {"rows": "1", "w0": "a3", "l0": "", "ws0": "2", "ls0": "0", "m0": "sb"})
    with pytest.raises(ops.AdminError, match="mode"):
        admin_web.add_from_form(s, r["id"], {"rows": "1", "w0": "a3", "l0": "a1", "ws0": "2", "ls0": "0", "m0": ""})
    assert not r["resolved"]


def test_old_flags_without_a_saved_question_are_guessed_from_the_message():
    s = make_state()
    s["review"].append({"id": 99, "kind": "unanswered", "reason": "No mode", "text": "2-1 to me against mika",
                        "sender": "a1", "ts": T0 + 9_000_000, "fight_id": None, "resolved": False})
    rows = ops.guess_rows(s, s["review"][-1], Ladder)
    assert (rows[0]["winner"], rows[0]["loser"], rows[0]["score"], rows[0]["mode"]) == ("a1", "a3", [2, 1], None)
    assert len(s["fights"]) == 2 and not s["pending"]          # the guess didn't touch the real state


def test_reask_posts_the_question_again_with_fresh_time():
    s = make_state()
    _, r = flag(s, "a3", "2-0", T0 + 9_000_000)
    now = T0 + 99_000_000
    msg = ops.reask(s, r["id"], Ladder, now)
    assert "Asked mika_09876 again" in msg and r["resolved"]
    assert s["outbox"][0].startswith("🔁 Asking again\n- mika_09876, who was that 2-0 against?")
    q = s["pending"][0]
    assert q["sender"] == "a3" and q["asked_at"] == now
    with pytest.raises(ops.AdminError, match="already"):
        ops.reask(s, r["id"], Ladder, now + 2)          # already re-asked
    s["review"].append(dict(r, id=50, resolved=False))
    with pytest.raises(ops.AdminError, match="question waiting"):
        ops.reask(s, 50, Ladder, now + 2)               # mika still has the re-asked question open


def test_bot_posts_the_outbox():
    from ladder.run import run

    class Chat:
        sent = []
        def identity(self): return "bot", "UFC_Rankings_Bot"
        def group_label(self): return "g", "UFC 4: Elites"
        def members(self): return list(PLAYERS.items())
        def messages(self, limit=100): return []
        def send(self, text): self.sent.append(text)

    s = make_state()
    s["meta"]["started"] = s["meta"]["intro_sent"] = s["meta"]["last_weekly"] = T0
    s["outbox"] = ["hello again"]
    run(Chat(), s, {"dry_run": False}, T0 + 1000)
    assert Chat.sent == ["hello again"] and "outbox" not in s


def test_flag_page_renders_forms(web):
    port, s, _ = web
    flag(s, "a2", "beat bjkid 2-1", T0 + 9_000_000)
    body = req(port, "GET", "/admin", cookie=login(port))[2]
    assert "Add result" in body and "Re-ask in chat" in body and 'class="need"' in body
