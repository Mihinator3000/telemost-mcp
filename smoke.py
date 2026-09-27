"""Read-only smoke test: calls every tool once through the MCP server and prints a short summary."""

import asyncio
import json
import sys

from telemost_mcp import server


def show(name: str, value) -> None:
    text = json.dumps(value, ensure_ascii=False, default=str)
    print(f"\n## {name}  ({len(value) if isinstance(value, list) else 1} item(s))\n{text[:700]}")


async def step(name: str, fn, *args, **kwargs):
    try:
        result = await fn(*args, **kwargs)
        show(name, result)
        return result
    except Exception as error:
        print(f"\n## {name}  FAILED: {type(error).__name__}: {error}"[:900])
        return None


async def main(chat_query: str) -> None:
    await step("whoami", server.whoami)
    chats = await step("chats_list", server.chats_list, limit=5)
    target = (await server.chats_list(query=chat_query, limit=1) or chats or [None])[0]
    if not target:
        return
    chat_id = target["chat_id"]
    print(f"\n>> target chat: {target['name']} ({chat_id})")
    await step("chat_info", server.chat_info, chat_id)
    page = await step("messages_get", server.messages_get, chat_id, limit=5)
    if page:
        await step("messages_get (older page)", server.messages_get, chat_id, before=page[0]["ts_mcs"], limit=3)
        starter = next((m for m in page if "thread" in m), None)
        if starter:
            await step("thread_get", server.thread_get, starter["thread"]["id"], limit=3)
        guids = sorted({m["author_guid"] for m in page if "author_guid" in m})
        await step("users_get", server.users_get, guids)
        word = next((w for m in page for w in m.get("text", "").split() if len(w) > 4), "привет")
        await step(f"messages_search '{word}'", server.messages_search, word, chat_id, limit=3)
    await step("sync (1 day)", server.sync, chat_id, days=1)
    if page:
        await step(f"local_search '{word}'", server.local_search, word, limit=3)


asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else ""))
