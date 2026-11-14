"""Connections to the group chat: the real PlayStation one, and a fake one for testing."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class PSNChat:
    def __init__(self, npsso: str, group_id: str = "", group_name: str = ""):
        from psnawp_api import PSNAWP  # imported here so tests don't need a login

        self.psn = PSNAWP(npsso)
        self.me = self.psn.me()
        self.group = self._find_group(group_id, group_name)
        self._info: dict[str, Any] | None = None

    def _find_group(self, group_id: str, group_name: str):
        if group_id:
            return self.psn.group(group_id=group_id)
        groups = list(self.me.get_groups(limit=50))
        named = []
        for g in groups:
            info = g.get_group_information()
            name = (info.get("groupName") or {}).get("value", "")
            if len(info.get("members", [])) > 2:
                named.append((g, name))
        if group_name:
            for g, name in named:
                if name.strip().lower() == group_name.strip().lower():
                    return g
        if len(named) == 1:
            return named[0][0]
        found = ", ".join(f"'{n}' ({g.group_id})" for g, n in named) or "none"
        raise RuntimeError(f"Can't tell which group chat to use. Group chats the bot is in: {found}")

    def info(self) -> dict[str, Any]:
        if self._info is None:
            self._info = self.group.get_group_information()
        return self._info

    def identity(self) -> tuple[str, str]:
        return self.me.account_id, self.me.online_id

    def group_label(self) -> tuple[str, str]:
        return self.group.group_id, (self.info().get("groupName") or {}).get("value", "")

    def members(self) -> list[tuple[str, str]]:
        return [(m["accountId"], m["onlineId"]) for m in self.info().get("members", [])]

    def messages(self, limit: int = 100) -> list[dict[str, Any]]:
        raw = self.group.get_conversation(limit=limit).get("messages", [])
        out = []
        for m in raw:
            if m.get("messageType") != 1:  # text only
                continue
            out.append({
                "uid": m["messageUid"], "body": m.get("body", ""), "ts": int(m["createdTimestamp"]),
                "sender_id": m["sender"]["accountId"], "sender_name": m["sender"].get("onlineId", ""),
            })
        return sorted(out, key=lambda m: m["ts"])

    def send(self, text: str) -> None:
        self.group.send_message(text)

    def direct_message(self, account_id: str, text: str) -> None:
        user = self.psn.user(account_id=account_id)
        self.psn.group(users_list=[user]).send_message(text)


class FakeChat:
    """Reads a JSON file of members and messages; records what the bot would send."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data = json.loads(self.path.read_text())
        self.sent: list[str] = []
        self.dms: list[tuple[str, str]] = []

    def identity(self) -> tuple[str, str]:
        return self.data["bot"]["account_id"], self.data["bot"]["online_id"]

    def group_label(self) -> tuple[str, str]:
        return "fake-group", self.data.get("group_name", "Test group")

    def members(self) -> list[tuple[str, str]]:
        return [(m["account_id"], m["online_id"]) for m in self.data["members"]]

    def messages(self, limit: int = 100) -> list[dict[str, Any]]:
        names = {m["account_id"]: m["online_id"] for m in self.data["members"]}
        out = []
        for i, m in enumerate(self.data["messages"]):
            out.append({"uid": m.get("uid", f"fake#{i}"), "body": m["body"], "ts": m["ts"],
                        "sender_id": m["from"], "sender_name": names.get(m["from"], m["from"])})
        return sorted(out, key=lambda m: m["ts"])[-limit:]

    def send(self, text: str) -> None:
        self.sent.append(text)

    def direct_message(self, account_id: str, text: str) -> None:
        self.dms.append((account_id, text))
