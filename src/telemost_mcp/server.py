"""MCP server: tools over Telemost chats (formerly Yandex Messenger).

Live tools call Telemost directly. `sync` copies history into an in-memory store, and
`local_search` runs prefix search over that copy across all synced chats. Set TELEMOST_DB to keep
the copy in a file between sessions, pruned to TELEMOST_RETENTION_DAYS (14 by default).

`message_send` is the only write. It posts only after the user approves the exact text and destination
in an MCP elicitation, so a client without elicitation cannot post at all.
"""

import asyncio
import os
import time
import uuid
from pathlib import Path
from typing import Annotated

from mcp.server.mcpserver import Elicit, ElicitationResult, MCPServer, Resolve
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from telemost_mcp.client import Telemost
from telemost_mcp.store import Store
from telemost_mcp.views import chat_view, iso, message_view, thread_id, to_mcs, user_view

mcp = MCPServer(
    "telemost",
    instructions=(
        "Access to the user's Yandex Telemost work chats. "
        "Find a chat_id with chats_list, then read it with messages_get or search it with messages_search. "
        "Messages that start a thread carry `thread` and inline `replies`; page long threads with thread_get. "
        "Timestamps are ISO 8601 in local time. "
        "message_send is the only write: it posts as the user after the user confirms it in the client."
    ),
)
client = Telemost()
db_path = os.environ.get("TELEMOST_DB")
store = Store(
    Path(db_path).expanduser() if db_path else None,
    retention_days=int(os.environ.get("TELEMOST_RETENTION_DAYS", "14")),
)
PAGE = 50
QUOTE_LENGTH = 200


async def all_chats() -> list[dict]:
    history = await client.fanout("history", {"Limit": 1, "ChatDataFilter": {}, "MinTimestamp": 0})
    return sorted(history["Chats"], key=lambda c: c.get("LastTsMcs", 0), reverse=True)


async def history_page(chat_id: str, before_mcs: int | None, limit: int) -> tuple[list[dict], dict]:
    """One page of a chat or thread, oldest first, plus the raw chat record (it holds a thread's parent)."""
    body = {"ChatId": chat_id, "Limit": limit} | ({"MaxTimestamp": before_mcs} if before_mcs else {})
    chat = next(iter((await client.fanout("history", body))["Chats"]), {})
    messages = [message_view(m["ServerMessage"]) for m in chat.get("Messages", [])]
    return sorted(messages, key=lambda m: m["ts_mcs"]), chat


async def chat_page(chat_id: str, before_mcs: int | None, limit: int) -> list[dict]:
    return (await history_page(chat_id, before_mcs, limit))[0]


async def attach_replies(messages: list[dict]) -> list[dict]:
    """Put the latest page of replies into each message that starts a thread."""
    parents = [m for m in messages if "thread" in m]
    for parent, replies in zip(parents, await asyncio.gather(*(chat_page(m["thread"]["id"], None, PAGE) for m in parents))):
        parent["replies"] = replies
    return messages


async def fetch_since(chat_id: str, since_mcs: int) -> list[dict]:
    messages, before = [], None
    while True:
        page = await chat_page(chat_id, before, PAGE)
        fresh = [m for m in page if m["ts_mcs"] >= since_mcs]
        messages += fresh
        if len(fresh) < PAGE:
            return messages
        before = page[0]["ts_mcs"]


@mcp.tool()
async def whoami() -> dict:
    """The logged-in user: name, email, uid, guid and organization."""
    await client.fanout("whoami", {})
    return client.me


@mcp.tool()
async def chats_list(query: str | None = None, limit: int = 50) -> list[dict]:
    """Chats sorted by last activity. `query` filters by a case-insensitive substring of the chat name."""
    chats = [chat_view(c) for c in await all_chats()]
    if query:
        chats = [c for c in chats if query.lower() in c["name"].lower()]
    return chats[:limit]


@mcp.tool()
async def chat_info(chat_id: str) -> dict:
    """Chat name, description, your role, and members with their positions and departments."""
    chat = (await client.api("get_chats_info", chat_ids=[chat_id]))["chats"][0]
    members = await client.api("get_users_data", guids=chat.get("members", [])[:200])
    return {
        "chat_id": chat_id,
        "name": chat.get("name"),
        "description": chat.get("description"),
        "my_role": chat.get("role"),
        "members_count": chat.get("members_count"),
        "members": [user_view(u) for u in members["users"]],
    }


@mcp.tool()
async def messages_get(
    chat_id: str, before: int | str | None = None, limit: int = PAGE, include_threads: bool = True
) -> list[dict]:
    """Messages of a chat in chronological order, newest page first.

    To page back, pass `ts_mcs` of the oldest returned message as `before`. An ISO 8601 time also works.
    With `include_threads`, each thread starter carries its latest 50 replies in `replies`.
    """
    messages = await chat_page(chat_id, to_mcs(before) if before else None, limit)
    if include_threads:
        await attach_replies(messages)
    return messages


@mcp.tool()
async def thread_get(thread_id: str, before: int | str | None = None, limit: int = PAGE) -> dict:
    """The parent message and the replies of a thread, oldest first.

    `thread_id` is `thread.id` of a message. To page back, pass the oldest reply's `ts_mcs` as `before`.
    """
    replies, chat = await history_page(thread_id, to_mcs(before) if before else None, limit)
    parent = chat.get("ThreadParentMessage")
    return {"parent": message_view(parent) if parent else None, "replies": replies}


@mcp.tool()
async def messages_search(query: str, chat_id: str | None = None, limit: int = 50) -> list[dict]:
    """Server-side search over the whole history, in one chat or, without `chat_id`, in all chats.

    Words match only in the exact form given, so search each likely form separately (релиз, релизы, релиза).
    The server returns at most 50 hits; narrow a crowded query with `chat_id`.
    """
    scope = {"chat_id": chat_id} if chat_id else {}
    result = await client.api("search", query=query, limit=limit, entities=["messages"], **scope)
    return [
        message_view(hit["data"]) | {"matches": hit.get("matches", {}).get("text")}
        for hit in result["messages"]["items"]
    ]


@mcp.tool()
async def users_get(guids: list[str]) -> list[dict]:
    """Name, email, position and department for user guids (for example `author_guid` from messages)."""
    return [user_view(u) for u in (await client.api("get_users_data", guids=guids))["users"]]


@mcp.tool()
async def sync(chat_id: str | None = None, days: int = 7) -> dict:
    """Load the last `days` of history, thread replies included, for `local_search`.

    Without `chat_id` it loads every chat and thread that had activity in that period.
    The loaded copy lasts for this session only.
    """
    since = int((time.time() - days * 86400) * 1e6)
    chats = await all_chats()
    names = {c["ChatId"]: chat_view(c)["name"] for c in chats if not chat_id or c["ChatId"] == chat_id}
    # A thread id is `<prefix>_<parent ts>`; the prefix maps the thread back to its chat.
    chat_of_prefix = {thread_id(cid, 0).rsplit("_", 1)[0]: cid for cid in names}
    # A late reply under an old message does not move the chat's LastTsMcs, so threads are listed separately.
    listed = (await client.fanout("history", {"Threads": True, "Limit": 0, "ChatDataFilter": {}, "MinTimestamp": since}))
    threads = {
        t["ChatId"]: f"{names[parent]} › thread"
        for t in listed["Chats"]
        if t.get("LastTsMcs", 0) >= since and (parent := chat_of_prefix.get(t["ChatId"].rsplit("_", 1)[0]))
    }
    synced: dict[str, int] = {}

    async def pull(cid: str, name: str) -> list[dict]:
        messages = await fetch_since(cid, since)
        store.replace(cid, since, name, messages)
        synced[name] = synced.get(name, 0) + len(messages)
        return messages

    for chat in chats:
        if chat["ChatId"] in names and chat.get("LastTsMcs", 0) >= since:
            name = names[chat["ChatId"]]
            for m in await pull(chat["ChatId"], name):
                if "thread" in m:
                    threads.setdefault(m["thread"]["id"], f"{name} › thread")
    for tid, name in threads.items():
        await pull(tid, name)
    store.prune()
    return {"since": iso(since), "threads": len(threads), "synced": synced, "store": store.stats()}


@mcp.tool()
async def local_search(query: str, chat_id: str | None = None, limit: int = 50) -> list[dict]:
    """Search messages loaded by `sync` across all chats. Words match as prefixes, so any word form hits.

    Run `sync` first. Prefer it to `messages_search` for "every mention in the last N days" questions.
    """
    return store.search(query, chat_id, limit)


class SendTarget(BaseModel):
    """Where a message goes, checked against the user's own chats before the user is asked."""

    chat_id: str
    where: str
    parent: dict | None = None
    reply: dict | None = None


class SendConfirmation(BaseModel):
    send: bool = Field(default=False, title="Отправить", description="Отметьте, чтобы сообщение ушло от вашего имени")


def excerpt(message: dict) -> str:
    text = message.get("text") or ", ".join(message.get("files", [])) or "(вложение)"
    return text if len(text) <= QUOTE_LENGTH else text[:QUOTE_LENGTH] + "…"


async def message_at(chat_id: str, ts_mcs: int) -> dict:
    page = await chat_page(chat_id, ts_mcs + 1, 1)
    if not page or page[-1]["ts_mcs"] != ts_mcs:
        raise ToolError(f"No message {ts_mcs} in {chat_id}")
    return page[-1]


def thread_owner(chat_id: str, names: dict[str, str]) -> str | None:
    """The chat a thread id belongs to: a thread id is `<prefix>_<parent ts>`, and the prefix maps to its chat."""
    prefix = chat_id.rsplit("_", 1)[0]
    return next((cid for cid in names if thread_id(cid, 0).rsplit("_", 1)[0] == prefix), None)


async def send_target(chat_id: str, thread_of: int | str | None, reply_to: int | str | None) -> SendTarget:
    names = {c["ChatId"]: chat_view(c)["name"] for c in await all_chats()}
    if chat_id in names:
        where = f"чат «{names[chat_id]}»"
    elif not thread_of and (owner := thread_owner(chat_id, names)):
        where = f"тред в чате «{names[owner]}»"
    else:
        raise ToolError("chat_id must be your chat, or, without thread_of, a thread of your chat")
    parent = await message_at(chat_id, to_mcs(thread_of)) if thread_of else None
    if parent:
        chat_id, where = thread_id(chat_id, parent["ts_mcs"]), f"тред в {where}"
    reply = await message_at(chat_id, to_mcs(reply_to)) if reply_to else None
    return SendTarget(chat_id=chat_id, where=where, parent=parent, reply=reply)


async def ask_to_send(target: Annotated[SendTarget, Resolve(send_target)], text: str) -> Elicit[SendConfirmation]:
    """Show the user exactly what is posted and where; only the user can approve it."""
    if not text.strip():
        raise ToolError("text is empty")
    lines = [f"Отправить от вашего имени в {target.where}?"]
    if target.parent:
        lines.append(f"Тред под сообщением — {target.parent.get('author')}: {excerpt(target.parent)}")
    if target.reply:
        lines.append(f"Ответ на сообщение — {target.reply.get('author')}: {excerpt(target.reply)}")
    lines += ["", "Текст:", text]
    return Elicit("\n".join(lines), SendConfirmation)


@mcp.tool()
async def message_send(
    chat_id: str,
    text: str,
    target: Annotated[SendTarget, Resolve(send_target)],
    confirmation: Annotated[ElicitationResult[SendConfirmation], Resolve(ask_to_send)],
    thread_of: int | str | None = None,
    reply_to: int | str | None = None,
) -> dict:
    """Post a text message as the user, after the user confirms the exact text and destination.

    The client shows the user a confirmation; nothing is posted unless the user approves it there.
    `chat_id` is a chat from chats_list or a thread id (`thread.id` of a message).
    `thread_of` is `ts_mcs` of a message in `chat_id`: the message goes into its thread, which starts if absent.
    `reply_to` is `ts_mcs` of a message in the destination to quote.
    After an error, read the chat before sending again: the message may already be posted.
    """
    if confirmation.action != "accept" or not confirmation.data.send:
        return {"sent": False, "reason": "the user did not confirm"}
    plain = {"ChatId": target.chat_id, "PayloadId": str(uuid.uuid4()), "Text": {"MessageText": text}}
    if target.reply:
        plain["ForwardedMessageRefs"] = [{"ChatId": target.chat_id, "Timestamp": target.reply["ts_mcs"]}]
        plain["ForwardedMessageStyles"] = [{"Quote": excerpt(target.reply)}]
    posted_mcs = ((await client.push(plain)).get("MessageInfo") or {}).get("TimestampMcs")
    return {"sent": True, "chat_id": target.chat_id, "ts_mcs": posted_mcs, "ts": iso(posted_mcs)}


def main() -> None:
    mcp.run()
