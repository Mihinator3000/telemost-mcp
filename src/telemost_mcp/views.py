"""Turns raw Telemost payloads into compact dicts for the model. Empty fields are dropped."""

from datetime import datetime, timezone


def iso(ts_mcs: int | None) -> str | None:
    return datetime.fromtimestamp(ts_mcs / 1e6, timezone.utc).astimezone().isoformat(timespec="seconds") if ts_mcs else None


def to_mcs(value: str | int) -> int:
    """Accept an exact `ts_mcs` (the lossless page cursor) or an ISO 8601 time."""
    if isinstance(value, int) or value.isdigit():
        return int(value)
    return int(datetime.fromisoformat(value).timestamp() * 1e6)


def compact(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in (None, "", [], False, 0)}


def thread_id(chat_id: str, ts_mcs: int) -> str:
    """Thread chat id, built as the web client does: group `N/Y/Z` -> `1NN/Y/Z_<ts>`, private -> `110/0/<chat>_<ts>`."""
    head, sep, rest = chat_id.partition("/")
    return f"{f'1{int(head):02d}/{rest}' if sep else f'110/0/{chat_id}'}_{ts_mcs}"


def file_names(plain: dict) -> list[str]:
    single = [plain[kind]["FileInfo"]["Name"] for kind in ("MiscFile", "Image") if kind in plain]
    gallery = [item["Image"]["FileInfo"]["Name"] for item in plain.get("Gallery", {}).get("Items", []) if "Image" in item]
    return single + gallery


def system_text(system: dict) -> str | None:
    if not system:
        return None
    diff = system.get("ParticipantsChangedDiff", {})
    parts = [f"added {u.get('DisplayName')}" for u in diff.get("AddedUsers", [])]
    parts += [f"removed {u.get('DisplayName')}" for u in diff.get("RemovedUsers", [])]
    if name := system.get("ChatInfoDiff", {}).get("Name"):
        parts.append(f"renamed the chat to «{name}»")
    return ", ".join(parts) or "system event"


def message_view(server_message: dict) -> dict:
    """`server_message` is `ServerMessage` from history or `data` from a search hit."""
    info = server_message["ServerMessageInfo"]
    client = server_message.get("ClientMessage", {})
    plain = client.get("Plain", {})
    author = info.get("From", {})
    chat_id = plain.get("ChatId") or client.get("SystemMessage", {}).get("ChatId")
    thread = info.get("ThreadState") or {}
    return compact({
        "chat_id": chat_id,
        "ts": iso(info["Timestamp"]),
        "ts_mcs": info["Timestamp"],
        "author": author.get("DisplayName"),
        "author_guid": author.get("Guid"),
        "text": plain.get("Text", {}).get("MessageText") or plain.get("Gallery", {}).get("Text"),
        "files": file_names(plain),
        "quotes": [s["Quote"] for s in plain.get("ForwardedMessageStyles", []) if s.get("Quote")],
        "forwarded": [
            f"{f['ServerMessageInfo']['From'].get('DisplayName')}: "
            + (f["Payload"].get("Text", {}).get("MessageText") or ", ".join(file_names(f["Payload"])) or "(attachment)")
            for f in server_message.get("ForwardedMessages", [])
        ],
        "system": system_text(client.get("SystemMessage")),
        "thread": {
            "id": thread_id(chat_id, info["Timestamp"]),
            "replies": thread["LastSeqNo"],
            "last_reply_at": iso(thread.get("LastTsMcs")),
        } if thread.get("LastSeqNo") else None,
        "deleted": info.get("Deleted"),
    })


def chat_view(chat: dict) -> dict:
    info = chat.get("ChatInfo") or {}
    partner = chat.get("PartnerInfo") or {}
    return compact({
        "chat_id": chat["ChatId"],
        "name": info.get("Name") or partner.get("DisplayName") or chat["ChatId"],
        "kind": "private" if partner else "group",
        "description": info.get("Description"),
        "members": info.get("MemberCount"),
        "last_message_at": iso(chat.get("LastTsMcs")),
        "unread": max(chat.get("LastSeqNo", 0) - chat.get("LastSeenByMeSeqNo", 0), 0),
        "muted": chat.get("Muted"),
    })


def user_view(user: dict) -> dict:
    employee = user.get("employee_info") or {}
    return compact({
        "guid": user["guid"],
        "name": user.get("display_name"),
        "email": user.get("email"),
        "position": employee.get("position"),
        "department": (employee.get("department") or {}).get("name"),
        "robot": user.get("is_robot"),
    })
