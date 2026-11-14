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


def test_html_in_names_is_escaped(web):
    port, s, _ = web
    s["players"]["a3"]["name"] = "<script>x</script>"
    body = req(port, "GET", "/admin", cookie=login(port))[2]
    assert "<script>x</script>" not in body and "&lt;script&gt;" in body


def test_password_hash_round_trip():
    h = admin_web.hash_password("abc def ghi")
    assert admin_web.check_password("abc def ghi", h) and not admin_web.check_password("abc def gh", h)
    assert not admin_web.check_password("x", "garbage")
