"""MCP server: read-only tools over Telemost chats (formerly Yandex Messenger).

Live tools call Telemost directly. `sync` copies history into a local SQLite store, and
`local_search` runs full-text search over that copy across all synced chats.
"""

import asyncio
import os
import time
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from telemost_mcp.client import Telemost
from telemost_mcp.store import Store
from telemost_mcp.views import chat_view, iso, message_view, thread_id, to_mcs, user_view

mcp = MCPServer(
    "telemost",
    instructions=(
        "Read-only access to the user's Yandex Telemost work chats. "
        "Find a chat_id with chats_list, then read it with messages_get or search it with messages_search. "
        "Messages that start a thread carry `thread` and inline `replies`; page long threads with thread_get. "
        "Timestamps are ISO 8601 in local time."
    ),
)
client = Telemost()
store = Store(Path(os.environ.get("TELEMOST_DB", "~/.local/share/telemost-mcp/messages.db")).expanduser())
PAGE = 50


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
    store.save(None, messages + [r for m in messages for r in m.get("replies", [])])  # `sync` adds chat names.
    return messages


@mcp.tool()
async def thread_get(thread_id: str, before: int | str | None = None, limit: int = PAGE) -> dict:
    """The parent message and the replies of a thread, oldest first.

    `thread_id` is `thread.id` of a message. To page back, pass the oldest reply's `ts_mcs` as `before`.
    """
    replies, chat = await history_page(thread_id, to_mcs(before) if before else None, limit)
    store.save(None, replies)
    parent = chat.get("ThreadParentMessage")
    return {"parent": message_view(parent) if parent else None, "replies": replies}


@mcp.tool()
async def messages_search(query: str, chat_id: str, limit: int = 50) -> list[dict]:
    """Server-side full-text search inside one chat. Covers the whole history, not only synced messages."""
    result = await client.api("search", query=query, chat_id=chat_id, limit=limit, entities=["messages"])
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
    """Copy the last `days` of history, thread replies included, into the local store for `local_search`.

    Without `chat_id` it syncs every chat and thread that had activity in that period.
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
        store.save(name, messages)
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
    return {"since": iso(since), "threads": len(threads), "synced": synced, "store": store.stats()}


@mcp.tool()
async def local_search(query: str, chat_id: str | None = None, limit: int = 50) -> list[dict]:
    """Full-text search over synced messages across all chats. Words match as prefixes. Run `sync` first."""
    return store.search(query, chat_id, limit)


def main() -> None:
    mcp.run()
