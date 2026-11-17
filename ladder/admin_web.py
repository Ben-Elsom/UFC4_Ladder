"""Private admin page, served from inside the bot process (ladder/serve.py).

Runs on 127.0.0.1 only; Caddy in front of it adds HTTPS. Only the admin's password gets in.
Edits change the bot's in-memory state under a shared lock, so the bot can't overwrite them,
and the normal save/publish loop rebuilds the website within a minute.

Set or change the password (asks twice, input hidden):

    .venv/bin/python -m ladder.admin_web set-password
"""

from __future__ import annotations

import getpass
import hashlib
import hmac
import html
import os
import secrets
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from . import admin_ops as ops
from .engine import MODES, Ladder

ENV_FILE = Path("/home/ubuntu/ladder_admin.env")
PORT = 8090
COOKIE = "ladder_admin"
SESSION_DAYS = 30
MAX_FAILS = 5                 # wrong passwords allowed ...
FAIL_WINDOW = 15 * 60         # ... in this many seconds, then logins are locked for the rest of the window
LOCK_WAIT = 20                # seconds to wait for the bot to finish a chat check before giving up


# ---------------------------------------------------------------- passwords
def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${h.hex()}"


def check_password(pw: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, want = stored.split("$")
        got = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=len(want) // 2)
        return kind == "scrypt" and hmac.compare_digest(got.hex(), want)
    except (ValueError, TypeError):
        return False


def set_password() -> None:
    pw = getpass.getpass("New admin password (typing is hidden): ")
    if len(pw) < 10:
        raise SystemExit("Use at least 10 characters. Nothing saved, run this again.")
    if getpass.getpass("Type it again: ") != pw:
        raise SystemExit("They didn't match. Nothing saved, run this again.")
    ENV_FILE.write_text(f"ADMIN_PASSWORD_HASH={hash_password(pw)}\nADMIN_SECRET={secrets.token_hex(32)}\n")
    os.chmod(ENV_FILE, 0o600)
    print("Saved. Restarting the bot so it takes effect...")


# ---------------------------------------------------------------- shared state
class Shared:
    """The bot's live state plus the lock both the chat loop and the admin page take before touching it."""

    def __init__(self, state: dict):
        self.state = state
        self.lock = threading.RLock()


# ---------------------------------------------------------------- page helpers
CSS = """
:root { --bg:#f4f1ec; --panel:#fff; --panel-2:#ece7df; --ink:#17140f; --muted:#6b645a; --line:#ddd5c9;
  --red:#c8261d; --red-soft:#f6dcd9; --green:#2f7d45; --green-soft:#dcefe1; --amber:#a35c00; --amber-soft:#fbe8cc; }
@media (prefers-color-scheme: dark) { :root { --bg:#0f0e0c; --panel:#191714; --panel-2:#23201c; --ink:#f1ece4;
  --muted:#9a9286; --line:#2f2b26; --red:#ff4a3d; --red-soft:#3a1714; --green:#5cc97a; --green-soft:#142a1b;
  --amber:#f0a33c; --amber-soft:#33230c; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 Inter, system-ui, sans-serif; }
.wrap { max-width:880px; margin:0 auto; padding:0 16px 64px; }
header { padding:24px 0 12px; display:flex; align-items:end; justify-content:space-between; gap:12px; flex-wrap:wrap; }
h1, h2 { font-family:"Barlow Condensed", Inter, sans-serif; }
h1 { margin:0; font-size:36px; line-height:1; font-weight:800; } h1 span { color:var(--red); }
h2 { font-size:24px; margin:26px 0 10px; font-weight:700; }
a { color:var(--red); }
.muted { color:var(--muted); } .small { font-size:13px; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:4px 16px; margin-bottom:12px; }
.row { display:grid; grid-template-columns:34px 1fr auto; gap:4px 12px; align-items:center; padding:12px 0; border-bottom:1px solid var(--line); }
.row:last-child { border-bottom:0; }
.row.void .what { opacity:.5; text-decoration:line-through; }
.fid { font:700 16px "Barlow Condensed", sans-serif; color:var(--muted); }
.what b { font-weight:600; }
.acts { display:flex; gap:6px; flex-wrap:wrap; justify-content:flex-end; }
.ctx { margin:6px 0 2px; font-size:13px; } .ctx summary { cursor:pointer; color:var(--muted); }
.ctxline { padding:3px 8px; border-left:2px solid var(--panel-2); margin:3px 0; overflow-wrap:anywhere; }
.msg { display:block; font:13px ui-monospace, Menlo, monospace; background:var(--panel-2); padding:4px 8px; border-radius:5px;
  margin-top:4px; white-space:pre-wrap; word-break:break-word; }
.pill { display:inline-block; font-size:12px; font-weight:600; padding:1px 8px; border-radius:99px; background:var(--panel-2); color:var(--muted); }
.pill.red { background:var(--red-soft); color:var(--red); } .pill.amber { background:var(--amber-soft); color:var(--amber); }
button, .btn { font:600 14px Inter, sans-serif; padding:8px 14px; border-radius:8px; border:1px solid var(--line);
  background:var(--panel); color:var(--ink); cursor:pointer; text-decoration:none; display:inline-block; min-height:38px; }
button.primary { background:var(--red); border-color:var(--red); color:#fff; }
button.danger { color:var(--red); }
input, select { font:15px Inter, sans-serif; padding:9px 10px; border-radius:8px; border:1px solid var(--line);
  background:var(--panel); color:var(--ink); width:100%; }
label { display:block; font-weight:600; font-size:13px; color:var(--muted); margin:14px 0 4px; }
.two { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
.banner { border-radius:10px; padding:10px 14px; margin:0 0 14px; font-weight:500; }
.banner.ok { background:var(--green-soft); color:var(--green); } .banner.bad { background:var(--red-soft); color:var(--red); }
.empty { color:var(--muted); padding:14px 0; }
.leaving { overflow:hidden; transition:opacity .25s ease, transform .25s ease, max-height .3s ease .05s, padding .3s ease .05s; }
.leaving.gone { opacity:0; transform:translateX(24px); max-height:0 !important; padding-top:0; padding-bottom:0; }
@media (prefers-reduced-motion: reduce) { .leaving { transition:none; } }
.flag { padding:14px 0; border-bottom:1px solid var(--line); } .flag:last-child { border-bottom:0; }
.fix { display:grid; grid-template-columns:1fr 1fr 120px 1fr; gap:8px 10px; margin:6px 0 10px; }
.fix label, .chk { margin:6px 0 3px; }
.chk { display:block; font-size:13px; font-weight:600; color:var(--ink); margin-top:12px; }
.chk input { width:auto; margin:0 4px 0 0; vertical-align:middle; }
.sc { display:flex; align-items:center; gap:4px; } .sc input { text-align:center; padding:9px 4px; }
select.need { border-color:var(--amber); background:var(--amber-soft); }
.acts.left { justify-content:flex-start; }
form.inline { display:inline; margin:0; }
@media (max-width:560px) { .fix { grid-template-columns:1fr 1fr; } .row { grid-template-columns:28px 1fr; } .acts { grid-column:2; justify-content:flex-start; } }
"""

FONTS = ('<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
         '<link href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@700;800&family=Inter:wght@400;600&display=swap" rel="stylesheet">')


def esc(x) -> str:
    return html.escape(str(x), quote=True)


def page(title: str, body: str) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">'
            f'<title>{esc(title)}</title>{FONTS}<style>{CSS}</style></head><body><div class="wrap">{body}</div>'
            f'<script>{LEAVE_JS}</script></body></html>')


# Removing a result or dismissing a flag: slide the row out, then submit (after the confirm box, if any).
LEAVE_JS = (
    "document.addEventListener('submit',e=>{const f=e.target;"
    "if(e.defaultPrevented||f.dataset.go||!/\\/(remove|dismiss)$/.test(f.getAttribute('action')||''))return;"
    "const row=f.closest('.row,.flag');if(!row||matchMedia('(prefers-reduced-motion: reduce)').matches)return;"
    "e.preventDefault();f.dataset.go=1;row.style.maxHeight=row.offsetHeight+'px';row.classList.add('leaving');"
    "row.offsetHeight;row.classList.add('gone');setTimeout(()=>f.submit(),330)})"
)


def when(ts: int | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts / 1000).strftime("%a %-d %b, %-I:%M%p").replace("AM", "am").replace("PM", "pm")


def score_text(f: dict) -> str:
    sc = f.get("score")
    return f" {sc[0]}–{sc[1]}" if sc else ""


# ---------------------------------------------------------------- the server
class AdminApp:
    def __init__(self, shared: Shared, pw_hash: str, secret: str, site_url: str = "", secure_cookie: bool = True,
                 make_ladder=None):
        self.shared, self.pw_hash, self.secret = shared, pw_hash, secret.encode()
        self.site_url, self.secure_cookie = site_url, secure_cookie
        self.make_ladder = make_ladder or (lambda st: Ladder(st))
        self.fails: list[float] = []
        self.fail_lock = threading.Lock()

    # sessions -------------------------------------------------------
    def _sign(self, text: str) -> str:
        return hmac.new(self.secret, f"{text}|{self.pw_hash}".encode(), hashlib.sha256).hexdigest()

    def new_session(self) -> str:
        exp = int(time.time()) + SESSION_DAYS * 86400
        return f"{exp}.{self._sign(f'session:{exp}')}"

    def valid_session(self, value: str | None) -> bool:
        if not value or "." not in value:
            return False
        exp, sig = value.split(".", 1)
        return exp.isdigit() and int(exp) > time.time() and hmac.compare_digest(sig, self._sign(f"session:{exp}"))

    def csrf(self, session: str) -> str:
        return self._sign(f"csrf:{session}")[:32]

    # login lockout ---------------------------------------------------
    def locked_for(self) -> int:
        with self.fail_lock:
            now = time.time()
            self.fails = [t for t in self.fails if now - t < FAIL_WINDOW]
            return int(self.fails[0] + FAIL_WINDOW - now) + 1 if len(self.fails) >= MAX_FAILS else 0

    def failed(self) -> None:
        with self.fail_lock:
            self.fails.append(time.time())

    # rendering -------------------------------------------------------
    def names(self, st: dict) -> dict[str, str]:
        return {a: p["name"] for a, p in st["players"].items()}

    def player_choices(self, st: dict) -> list[tuple[str, str]]:
        bot = st.get("bot", {}).get("account_id")
        fought = {x for f in st["fights"] for x in (f["winner"], f["loser"])}
        ids = [a for a, p in st["players"].items() if a != bot and (p.get("in_group", True) or a in fought)]
        return sorted(((a, st["players"][a]["name"]) for a in ids), key=lambda t: t[1].lower())

    def describe(self, f: dict, n: dict[str, str]) -> str:
        sc = f.get("score")
        w, lo = esc(n.get(f["winner"], "?")), esc(n.get(f["loser"], "?"))
        if sc and sc[0] == sc[1]:
            return f"<b>{w}</b> and <b>{lo}</b> split {sc[0]}–{sc[1]}"
        return f"<b>{w}</b> def. <b>{lo}</b>{score_text(f)}"

    def flag_card(self, st: dict, r: dict, n: dict[str, str], btn, tok: str) -> str:
        """One flagged message: what the bot knew, a form to finish the result off, re-ask and dismiss."""
        rid = r["id"]
        who = esc(n.get(r.get("sender"), "")) if r.get("sender") else ""
        head = (f'<div class="what"><b>{esc(r["reason"])}</b> <span class="muted small">R{rid} · {esc(when(r.get("ts")))}</span></div>'
                f'<span class="msg">{who}{": " if who else ""}{esc(r.get("text", ""))}</span>')
        uid = (r.get("question") or {}).get("uid")
        before = [m for m in st.get("chat_log", []) if m["ts"] < (r.get("ts") or 0) and m["uid"] != uid][-5:]
        if before:
            head += ('<details class="ctx"><summary>Earlier messages</summary>' + "".join(
                f'<div class="ctxline"><span class="muted small">{esc(when(m["ts"]))}</span> '
                f'<b>{esc(n.get(m["sender_id"]) or m.get("sender_name") or "?")}</b>: {esc(m.get("body") or "")}</div>'
                for m in before) + "</details>")
        dismiss = btn(f"/admin/review/{rid}/dismiss", "Dismiss")
        if r.get("fight_id"):
            return (f'<div class="flag">{head}<p class="small muted">This is about result {r["fight_id"]}. '
                    f'Edit it to set what really happened, then dismiss this.</p>'
                    f'<div class="acts left"><a class="btn" href="/admin/fight/{r["fight_id"]}">Edit result {r["fight_id"]}</a>{dismiss}</div></div>')
        choices = self.player_choices(st)
        rows = ops.guess_rows(st, r, self.make_ladder)

        def people(sel: str | None, suggested: list[str]) -> str:
            out = '<option value="">Pick a player</option>' if not sel else ""
            sug = [a for a in suggested if a in n]
            if sug:
                out += '<optgroup label="Did they mean">' + "".join(
                    f'<option value="{esc(a)}"{" selected" if a == sel else ""}>{esc(n[a])}</option>' for a in sug) + "</optgroup>"
                out += '<optgroup label="Everyone">'
            out += "".join(f'<option value="{esc(a)}"{" selected" if a == sel and a not in sug else ""}>{esc(nm)}</option>'
                           for a, nm in choices)
            return out + ("</optgroup>" if sug else "")

        fields = []
        for i, row in enumerate(rows):
            sc = row.get("score") or ["", ""]
            need = lambda v: ' class="need"' if not v else ""          # noqa: E731
            modes = ('<option value="">Pick mode</option>' if not row.get("mode") else "") + "".join(
                f'<option value="{k}"{" selected" if k == row.get("mode") else ""}>{v}</option>' for k, v in MODES.items())
            tick = (f'<label class="chk"><input type="checkbox" name="inc{i}" value="1" checked> '
                    f'Result {i + 1}: <span class="muted">{esc(row.get("text", ""))}</span></label>') if len(rows) > 1 else ""
            fields.append(
                f'{tick}<div class="fix">'
                f'<div><label>Winner</label><select name="w{i}"{need(row.get("winner"))}>{people(row.get("winner"), row.get("options", []))}</select></div>'
                f'<div><label>Loser</label><select name="l{i}"{need(row.get("loser"))}>{people(row.get("loser"), row.get("options", []))}</select></div>'
                f'<div><label>Score</label><div class="sc"><input name="ws{i}" type="number" min="0" max="20" inputmode="numeric" value="{esc(sc[0])}">'
                f'<span>–</span><input name="ls{i}" type="number" min="0" max="20" inputmode="numeric" value="{esc(sc[1])}"></div></div>'
                f'<div><label>Mode</label><select name="m{i}"{need(row.get("mode"))}>{modes}</select></div></div>')
        can_reask = bool(r.get("sender") and r.get("text"))
        reask = btn(f"/admin/review/{rid}/reask", "Re-ask in chat", "",
                    f"Post the question in the group chat again for {n.get(r.get('sender'), 'them')}?") if can_reask else ""
        return (f'<div class="flag">{head}'
                f'<form id="fix{rid}" method="post" action="/admin/review/{rid}/add"><input type="hidden" name="csrf" value="{tok}">'
                f'<input type="hidden" name="rows" value="{len(rows)}">{"".join(fields)}</form>'
                f'<div class="acts left"><button class="primary" form="fix{rid}">Add result</button>{reask}{dismiss}</div></div>')

    def dashboard(self, session: str, note: str = "", bad: bool = False) -> str:
        tok = esc(self.csrf(session))
        with self.shared.lock:
            st = self.shared.state
            n = self.names(st)
            fights = list(reversed(st["fights"]))
            reviews = [r for r in st["review"] if not r["resolved"]]

            def btn(action: str, label: str, cls: str = "", confirm: str = "") -> str:
                on = f' onsubmit="return confirm(\'{esc(confirm)}\')"' if confirm else ""
                return (f'<form class="inline" method="post" action="{action}"{on}><input type="hidden" name="csrf" value="{tok}">'
                        f'<button class="{cls}">{label}</button></form>')

            rows = []
            for f in fights:
                st_pill = {"void": '<span class="pill red">Removed</span> ', "disputed": '<span class="pill amber">Doesn\'t match</span> '}.get(f["status"], "")
                if f["status"] == "void" and f.get("disputes"):
                    st_pill = f'<span class="pill red">Disputed by {esc(n.get(f["disputes"][-1]["by"], "?"))}</span> '
                elif f.get("disputes"):
                    st_pill = f'<span class="pill amber">Disputed {len(f["disputes"])}×, put back</span> '

                by = esc(n.get(f.get("reported_by"), "admin"))
                text = f'<span class="msg">{by}: {esc(f.get("text", ""))}</span>' if f.get("text") else ""
                acts = (btn(f"/admin/fight/{f['id']}/restore", "Undo remove") if f["status"] == "void" else
                        f'<a class="btn" href="/admin/fight/{f["id"]}">Edit</a>'
                        + btn(f"/admin/fight/{f['id']}/resend", "Resend", "",
                              f"Post result {f['id']} in the group chat again, with its points?")
                        + btn(f"/admin/fight/{f['id']}/remove", "Remove", "danger",
                              f"Remove result {f['id']}? You can undo this."))
                search = esc(f"{n.get(f['winner'], '')} {n.get(f['loser'], '')} {MODES.get(f['mode'], '')} {f.get('text', '')}".lower())
                rows.append(f'<div class="row{" void" if f["status"] == "void" else ""}" data-s="{search}">'
                            f'<div class="fid">{f["id"]}</div>'
                            f'<div><div class="what">{st_pill}{self.describe(f, n)}</div>'
                            f'<div class="muted small">{esc(MODES.get(f["mode"], f["mode"]))} · {esc(when(f["ts"]))}</div>{text}</div>'
                            f'<div class="acts">{acts}</div></div>')

            rev = [self.flag_card(st, r, n, btn, tok) for r in reviews]
            disputes: dict[str, int] = {}
            for f in st["fights"]:
                for d in f.get("disputes", []):
                    disputes[d["by"]] = disputes.get(d["by"], 0) + 1

        live = sum(1 for f in fights if f["status"] != "void")
        site = f' · <a href="{esc(self.site_url)}">Public site</a>' if self.site_url else ""
        banner = f'<div class="banner {"bad" if bad else "ok"}">{esc(note)}</div>' if note else ""
        body = (f'<header><div><h1>UFC 4 <span>Ladder</span> admin</h1>'
                f'<div class="muted small">{live} results counted{site}</div></div>'
                f'<form class="inline" method="post" action="/admin/logout"><input type="hidden" name="csrf" value="{tok}"><button>Log out</button></form></header>'
                f'{banner}')
        if rev:
            body += (f'<h2>Flagged messages ({len(rev)})</h2><p class="muted small" style="margin-top:-6px">Fill in what\'s missing '
                     f'(highlighted) and add it, ask the player again in the chat, or dismiss it.</p><div class="card">{"".join(rev)}</div>')
        if disputes:
            top = sorted(disputes.items(), key=lambda x: (-x[1], n.get(x[0], "").lower()))
            body += ('<h2>Disputes</h2><div class="card">' + "".join(
                f'<div class="row"><div class="fid">{c}</div><div><div class="what">{esc(n.get(a, "?"))}</div></div></div>'
                for a, c in top) + '</div>')
        body += (f'<h2>Results</h2><input id="q" type="search" placeholder="Search by name, mode or message" autocomplete="off">'
                 f'<div style="height:10px"></div><div class="card" id="list">'
                 f'{"".join(rows) or "<div class=empty>No results yet</div>"}</div>'
                 '<script>document.getElementById("q").addEventListener("input",e=>{const q=e.target.value.toLowerCase().trim();'
                 'document.querySelectorAll("#list .row").forEach(r=>{r.style.display=!q||r.dataset.s.includes(q)?"":"none"})})</script>')
        return page("Ladder admin", body)

    def edit_page(self, session: str, fid: str, err: str = "", form: dict | None = None) -> str | None:
        with self.shared.lock:
            st = self.shared.state
            try:
                f = ops.find_fight(st, fid)
            except ops.AdminError:
                return None
            f = dict(f)
            choices = self.player_choices(st)
            n = self.names(st)
        form = form or {}
        sc = f.get("score") or ["", ""]
        cur = {"winner": form.get("winner", f["winner"]), "loser": form.get("loser", f["loser"]),
               "ws": form.get("ws", sc[0]), "ls": form.get("ls", sc[1]), "mode": form.get("mode", f["mode"])}
        for side in ("winner", "loser"):                 # keep ex-members selectable for old results
            if cur[side] not in dict(choices) and cur[side] in n:
                choices.append((cur[side], n[cur[side]]))

        def opts(sel: str) -> str:
            return "".join(f'<option value="{esc(a)}"{" selected" if a == sel else ""}>{esc(nm)}</option>' for a, nm in choices)

        modes = "".join(f'<option value="{k}"{" selected" if k == cur["mode"] else ""}>{v}</option>' for k, v in MODES.items())
        banner = f'<div class="banner bad">{esc(err)}</div>' if err else ""
        text = f'<span class="msg">{esc(n.get(f.get("reported_by"), "admin"))}: {esc(f.get("text", ""))}</span>' if f.get("text") else ""
        removed = '<p><span class="pill red">Removed</span> This result is removed. Saving keeps it removed; use Undo remove on the list to count it again.</p>' if f["status"] == "void" else ""
        body = (f'<header><div><h1>Edit result {f["id"]}</h1><div class="muted small">{esc(when(f["ts"]))}</div></div>'
                f'<a class="btn" href="/admin">Back</a></header>{banner}{removed}'
                f'<div class="card" style="padding:12px 16px"><div class="muted small">Original message</div>{text or "<span class=muted>Added by admin</span>"}</div>'
                f'<form method="post" action="/admin/fight/{f["id"]}"><input type="hidden" name="csrf" value="{esc(self.csrf(session))}">'
                f'<div class="two"><div><label for="w">Winner</label><select id="w" name="winner">{opts(cur["winner"])}</select></div>'
                f'<div><label for="l">Loser</label><select id="l" name="loser">{opts(cur["loser"])}</select></div></div>'
                f'<div class="two"><div><label for="ws">Winner\'s score</label><input id="ws" name="ws" type="number" min="0" max="20" inputmode="numeric" value="{esc(cur["ws"])}"></div>'
                f'<div><label for="ls">Loser\'s score</label><input id="ls" name="ls" type="number" min="0" max="20" inputmode="numeric" value="{esc(cur["ls"])}"></div></div>'
                f'<p class="muted small">Same number on both sides = a split. Leave both blank for no score.</p>'
                f'<label for="m">Mode</label><select id="m" name="mode">{modes}</select>'
                f'<div style="margin-top:20px;display:flex;gap:8px"><button class="primary">Save</button>'
                f'{"" if f["status"] == "void" else "<button name=resend value=1 title=\"Save, then post a correction with the updated points in the group chat\">Edit and Resend</button>"}'
                f'<a class="btn" href="/admin">Cancel</a></div></form>')
        return page(f"Edit result {f['id']}", body)

    def login_page(self, err: str = "") -> str:
        banner = f'<div class="banner bad">{esc(err)}</div>' if err else ""
        return page("Ladder admin", (
            f'<header><h1>UFC 4 <span>Ladder</span> admin</h1></header>{banner}'
            '<form method="post" action="/admin/login" class="card" style="padding:16px;max-width:380px">'
            '<label for="pw" style="margin-top:0">Password</label>'
            '<input id="pw" name="password" type="password" autocomplete="current-password" autofocus required>'
            '<div style="margin-top:14px"><button class="primary">Log in</button></div></form>'))

    # actions ---------------------------------------------------------
    def act(self, fn) -> tuple[bool, str]:
        if not self.shared.lock.acquire(timeout=LOCK_WAIT):
            return False, "The bot is busy checking the chat. Try again in a few seconds."
        try:
            return True, fn(self.shared.state)
        except ops.AdminError as exc:
            return False, str(exc)
        finally:
            self.shared.lock.release()

    def handler(self):
        app = self

        class H(BaseHTTPRequestHandler):
            server_version = "ladder"
            sys_version = ""

            def log_message(self, fmt, *args):          # keep the bot's log readable
                pass

            def send(self, code: int, body: str = "", headers: dict | None = None) -> None:
                data = body.encode()
                self.send_response(code)
                for k, v in {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store",
                             "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin",
                             "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline' https://fonts.googleapis.com; "
                                                        "font-src https://fonts.gstatic.com; script-src 'unsafe-inline'; "
                                                        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
                             **(headers or {})}.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def go(self, where: str, cookie: str | None = None) -> None:
                h = {"Location": where}
                if cookie is not None:
                    h["Set-Cookie"] = cookie
                self.send(303, "", h)

            def session(self) -> str | None:
                for part in (self.headers.get("Cookie") or "").split(";"):
                    k, _, v = part.strip().partition("=")
                    if k == COOKIE and app.valid_session(v):
                        return v
                return None

            def cookie(self, value: str, max_age: int) -> str:
                sec = "; Secure" if app.secure_cookie else ""
                return f"{COOKIE}={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Strict{sec}"

            def do_GET(self):
                path = urlparse(self.path).path.rstrip("/") or "/"
                qs = parse_qs(urlparse(self.path).query)
                s = self.session()
                if path in ("/", "/admin") and not s:
                    return self.go("/admin/login")
                if path == "/admin/login":
                    return self.go("/admin") if s else self.send(200, app.login_page())
                if not s:
                    return self.go("/admin/login")
                if path in ("/", "/admin"):
                    note = (qs.get("ok") or qs.get("err") or [""])[0]
                    return self.send(200, app.dashboard(s, note[:200], bad="err" in qs))
                parts = path.split("/")
                if len(parts) == 4 and parts[2] == "fight":
                    out = app.edit_page(s, parts[3])
                    return self.send(200, out) if out else self.send(404, page("Not found", "<p>No such result. <a href='/admin'>Back</a></p>"))
                self.send(404, page("Not found", "<p>Not found. <a href='/admin'>Back</a></p>"))

            def do_POST(self):
                path = urlparse(self.path).path.rstrip("/")
                size = min(int(self.headers.get("Content-Length") or 0), 10_000)
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(size).decode(errors="replace")).items()}
                origin = self.headers.get("Origin")
                host = self.headers.get("Host", "")
                if origin and urlparse(origin).netloc != host:
                    return self.send(403, page("Blocked", "<p>Request blocked.</p>"))

                if path == "/admin/login":
                    wait = app.locked_for()
                    if wait:
                        return self.send(429, app.login_page(f"Too many wrong passwords. Try again in {wait // 60 + 1} minutes."))
                    if check_password(form.get("password", ""), app.pw_hash):
                        with app.fail_lock:
                            app.fails.clear()
                        return self.go("/admin", self.cookie(app.new_session(), SESSION_DAYS * 86400))
                    app.failed()
                    time.sleep(1)
                    return self.send(401, app.login_page("Wrong password."))

                s = self.session()
                if not s:
                    return self.go("/admin/login")
                if not hmac.compare_digest(form.get("csrf", ""), app.csrf(s)):
                    return self.send(403, page("Blocked", "<p>That form expired. <a href='/admin'>Go back</a> and try again.</p>"))
                if path == "/admin/logout":
                    return self.go("/admin/login", self.cookie("", 0))

                parts = path.split("/")       # ['', 'admin', 'fight', '14', 'remove']
                ok, msg = False, "Unknown action"
                if len(parts) == 5 and parts[2] == "fight" and parts[4] == "remove":
                    ok, msg = app.act(lambda st: (ops.void(st, parts[3]), f"Removed result {parts[3]}")[1])
                elif len(parts) == 5 and parts[2] == "fight" and parts[4] == "resend":
                    ok, msg = app.act(lambda st: (ops.correction(st, parts[3], None, app.make_ladder),
                                                  f"Result {parts[3]} will post in the chat within a minute or two")[1])
                elif len(parts) == 5 and parts[2] == "fight" and parts[4] == "restore":
                    ok, msg = app.act(lambda st: (ops.restore(st, parts[3]), f"Result {parts[3]} counts again")[1])
                elif len(parts) == 5 and parts[2] == "review" and parts[4] == "reask":
                    ok, msg = app.act(lambda st: ops.reask(st, parts[3], app.make_ladder))
                elif len(parts) == 5 and parts[2] == "review" and parts[4] == "add":
                    ok, msg = app.act(lambda st: add_from_form(st, parts[3], form))
                elif len(parts) == 5 and parts[2] == "review" and parts[4] == "dismiss":
                    ok, msg = app.act(lambda st: (ops.resolve_review(st, parts[3], "dismissed on admin page"), "Dismissed")[1])
                elif len(parts) == 4 and parts[2] == "fight":
                    ws, ls = form.get("ws", "").strip(), form.get("ls", "").strip()
                    resend = form.get("resend") == "1"

                    def save(st):
                        if bool(ws) != bool(ls):
                            raise ops.AdminError("Fill in both scores, or leave both blank")
                        try:
                            score = [int(ws), int(ls)] if ws else None
                        except ValueError:
                            raise ops.AdminError("Scores must be whole numbers") from None
                        was = ops.describe(st, ops.find_fight(st, parts[3]), app.make_ladder)
                        if resend and ops.find_fight(st, parts[3])["status"] == "void":
                            raise ops.AdminError("This result is removed, so there's nothing to resend")
                        ops.edit(st, parts[3], winner=form.get("winner"), loser=form.get("loser"),
                                 score=score, mode=form.get("mode"))
                        if resend:
                            ops.correction(st, parts[3], was, app.make_ladder)
                            return f"Saved result {parts[3]}. The correction will post in the chat within a minute or two"
                        return f"Saved result {parts[3]}"

                    ok, msg = app.act(save)
                    if not ok:
                        out = app.edit_page(s, parts[3], msg, form)
                        return self.send(400, out) if out else self.go("/admin?err=" + quote(msg))
                self.go("/admin?" + ("ok=" if ok else "err=") + quote(msg))

        return H


def add_from_form(st: dict, rid: str, form: dict) -> str:
    r = ops.find_review(st, rid)
    if r["resolved"]:
        raise ops.AdminError("That one's already sorted")
    try:
        count = max(1, min(10, int(form.get("rows", "1"))))
    except ValueError:
        count = 1
    todo = []
    for i in range(count):
        if count > 1 and not form.get(f"inc{i}"):
            continue
        which = f" for result {i + 1}" if count > 1 else ""
        w, lo, mode = form.get(f"w{i}") or None, form.get(f"l{i}") or None, form.get(f"m{i}") or None
        ws, ls = form.get(f"ws{i}", "").strip(), form.get(f"ls{i}", "").strip()
        if not w or not lo:
            raise ops.AdminError(f"Pick both players{which}")
        if not mode:
            raise ops.AdminError(f"Pick the mode{which}")
        if bool(ws) != bool(ls):
            raise ops.AdminError(f"Fill in both scores{which}, or leave both blank")
        try:
            score = [int(ws), int(ls)] if ws else None
        except ValueError:
            raise ops.AdminError(f"Scores must be whole numbers{which}") from None
        ops._check(st, w, lo, score, mode)
        todo.append((i, w, lo, score, mode))
    if not todo:
        raise ops.AdminError("Tick at least one result to add")
    ids = [str(ops.add_result(st, winner=w, loser=lo, score=score, mode=mode, ts=r.get("ts"), text=r.get("text", ""),
                              reported_by=r.get("sender"), uid=f"admin-r{r['id']}-{i}")["id"]) for i, w, lo, score, mode in todo]
    r["resolved"], r["resolution"] = True, f"added as result {', '.join(ids)}"
    return f"Added result {' and '.join(ids)}"


def start(shared: Shared, site_url: str = "", port: int = PORT, log=print, make_ladder=None) -> ThreadingHTTPServer | None:
    pw_hash, secret = os.environ.get("ADMIN_PASSWORD_HASH", ""), os.environ.get("ADMIN_SECRET", "")
    if not pw_hash or not secret:
        log("admin page off: no password set (run deploy/set_admin_password.sh)")
        return None
    srv = ThreadingHTTPServer(("127.0.0.1", port), AdminApp(shared, pw_hash, secret, site_url, make_ladder=make_ladder).handler())
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="admin-web", daemon=True).start()
    log(f"admin page listening on 127.0.0.1:{port}")
    return srv


if __name__ == "__main__":
    if sys.argv[1:] == ["set-password"]:
        set_password()
    else:
        raise SystemExit(__doc__)
