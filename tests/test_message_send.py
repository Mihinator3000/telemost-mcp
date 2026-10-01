"""Offline tests of the message_send confirmation gate: Telemost is replaced by fakes, nothing leaves the process."""

import json

import pytest
from mcp import Client
from mcp.shared.exceptions import MCPError
from mcp_types import ElicitResult

from telemost_mcp import server

GROUP = "0/0/group"
PRIVATE = "guid-a_guid-b"
PARENT_TS = 1790000000000000
REPLY_TS = 1790000000500000
MESSAGES = {
    GROUP: [{"chat_id": GROUP, "ts_mcs": PARENT_TS, "author": "Иван", "text": "Выкатили релиз"}],
    "100/0/group_1790000000000000": [{"ts_mcs": REPLY_TS, "author": "Пётр", "text": "Проверил"}],
}
MODES = ["legacy", "2026-07-28"]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def pushed(monkeypatch):
    sent: list[dict] = []

    async def all_chats():
        return [
            {"ChatId": GROUP, "ChatInfo": {"Name": "Релизы"}},
            {"ChatId": PRIVATE, "PartnerInfo": {"DisplayName": "Пётр"}},
        ]

    async def chat_page(chat_id, before_mcs, limit):
        return [m for m in MESSAGES.get(chat_id, []) if before_mcs is None or m["ts_mcs"] < before_mcs][-limit:]

    async def push(plain):
        sent.append(plain)
        return {"Status": 1, "MessageInfo": {"TimestampMcs": 1790000001000000}}

    monkeypatch.setattr(server, "all_chats", all_chats)
    monkeypatch.setattr(server, "chat_page", chat_page)
    monkeypatch.setattr(server.client, "push", push)
    return sent


def answering(action, send=None, asked=None):
    async def callback(context, params):
        if asked is not None:
            asked.append(params.message)
        return ElicitResult(action=action, content=None if send is None else {"send": send})

    return callback


async def call(mode, callback, **arguments):
    async with Client(server.mcp, mode=mode, elicitation_callback=callback) as client:
        return await client.call_tool("message_send", arguments)


def outcome(result):
    return json.loads(result.content[0].text)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_client_without_elicitation_cannot_post(pushed, mode):
    with pytest.RaisesGroup(pytest.RaisesExc(MCPError, match="elicitation capability"), flatten_subgroups=True):
        await call(mode, None, chat_id=GROUP, text="привет")

    assert pushed == []


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(("action", "send"), [("decline", None), ("cancel", None), ("accept", False)])
async def test_unconfirmed_message_is_not_posted(pushed, mode, action, send):
    result = await call(mode, answering(action, send), chat_id=GROUP, text="привет")

    assert outcome(result) == {"sent": False, "reason": "the user did not confirm"}
    assert pushed == []


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_confirmation_shows_destination_and_text(pushed, mode):
    asked: list[str] = []

    await call(mode, answering("accept", True, asked), chat_id=GROUP, text="Релиз в 18:00")

    assert len(asked) == 1
    assert "чат «Релизы»" in asked[0]
    assert asked[0].endswith("Релиз в 18:00")


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_confirmed_message_is_posted_once(pushed, mode):
    result = await call(mode, answering("accept", True), chat_id=GROUP, text="привет")

    assert outcome(result)["sent"] is True
    assert [p["ChatId"] for p in pushed] == [GROUP]
    assert pushed[0]["Text"] == {"MessageText": "привет"}
    assert pushed[0]["PayloadId"]


@pytest.mark.anyio
async def test_thread_of_posts_into_the_thread_of_that_message(pushed):
    asked: list[str] = []

    await call("legacy", answering("accept", True, asked), chat_id=GROUP, text="ок", thread_of=PARENT_TS)

    assert pushed[0]["ChatId"] == "100/0/group_1790000000000000"
    assert "тред в чат «Релизы»" in asked[0]
    assert "Иван: Выкатили релиз" in asked[0]


@pytest.mark.anyio
async def test_thread_id_posts_into_that_thread_with_a_quote(pushed):
    thread = "100/0/group_1790000000000000"

    await call("legacy", answering("accept", True), chat_id=thread, text="+1", reply_to=REPLY_TS)

    assert pushed[0]["ChatId"] == thread
    assert pushed[0]["ForwardedMessageRefs"] == [{"ChatId": thread, "Timestamp": REPLY_TS}]
    assert pushed[0]["ForwardedMessageStyles"] == [{"Quote": "Проверил"}]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "arguments",
    [
        {"chat_id": "0/0/stranger", "text": "привет"},
        {"chat_id": "100/0/group_1790000000000000", "text": "привет", "thread_of": REPLY_TS},
        {"chat_id": GROUP, "text": "привет", "thread_of": 123},
    ],
)
async def test_unknown_destination_is_refused_before_asking(pushed, arguments):
    asked: list[str] = []

    result = await call("legacy", answering("accept", True, asked), **arguments)

    assert result.is_error
    assert asked == []
    assert pushed == []
