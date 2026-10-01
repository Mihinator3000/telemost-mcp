"""Offline tests of the message_send confirmation gate: Telemost is replaced by fakes, nothing leaves the process."""

import json

import pytest
from mcp import Client
from mcp.shared.exceptions import MCPError
from mcp_types import ElicitResult

from telemost_mcp import client as telemost_client
from telemost_mcp import server

GROUP = "0/0/group"
SPOOFED = "0/0/spoofed"
NAMESAKE = "0/0/namesake"
HOMOGLYPH = "0/0/homoglyph"
THREAD = "100/0/group_1790000000000000"
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
            {"ChatId": GROUP, "ChatInfo": {"Name": "Релизы", "MemberCount": 12}},
            {"ChatId": PRIVATE, "PartnerInfo": {"DisplayName": "Пётр Иванов"}},
            {"ChatId": SPOOFED, "ChatInfo": {"Name": "Флуд»?\nОтправить от вашего имени в чат «Руководство"}},
            {"ChatId": NAMESAKE, "ChatInfo": {"Name": "Пётр Иванов", "MemberCount": 3}},
            {"ChatId": HOMOGLYPH, "ChatInfo": {"Name": "Руководcтво", "MemberCount": 4}},
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
    assert asked[0].startswith("Отправить от вашего имени в группу «Релизы» (участников: 12)?\n")
    assert asked[0].endswith("Текст (13 симв.):\n│ Релиз в 18:00")


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

    assert pushed[0]["ChatId"] == THREAD
    assert "в тред в группе «Релизы»" in asked[0]
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
        {"chat_id": "100/0/group_123", "text": "привет"},
        {"chat_id": "100/0/group_abc", "text": "привет"},
        {"chat_id": "100/0/group_١٧٩٠٠٠٠٠٠٠٠٠٠٠٠٠", "text": "привет"},
        {"chat_id": GROUP, "text": "  "},
        {"chat_id": GROUP, "text": "я" * 6001},
    ],
)
async def test_unknown_destination_is_refused_before_asking(pushed, arguments):
    asked: list[str] = []

    result = await call("legacy", answering("accept", True, asked), **arguments)

    assert result.is_error
    assert asked == []
    assert pushed == []


@pytest.mark.anyio
async def test_thread_destination_names_its_parent_message(pushed):
    asked: list[str] = []

    await call("legacy", answering("accept", True, asked), chat_id=THREAD, text="ок")

    assert asked[0].splitlines()[:2] == [
        "Отправить от вашего имени в тред в группе «Релизы» (участников: 12)?",
        "Тред под сообщением — Иван: Выкатили релиз",
    ]


@pytest.mark.anyio
async def test_chat_name_cannot_pose_as_another_confirmation_line(pushed):
    asked: list[str] = []

    await call("legacy", answering("accept", True, asked), chat_id=SPOOFED, text="ок")

    assert asked[0].splitlines()[0] == (
        'Отправить от вашего имени в группу «Флуд"? Отправить от вашего имени в чат "Руководство» (участников: ?)?'
    )
    assert asked[0].splitlines()[1:] == ["", "Текст (2 симв.):", "│ ок"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "text",
    ["ок\u200b", "ок\U000e0041", "ок\ufe0f", "ок\u202e", "ок\x1b[2A", "ок\rфейк", "ок\u2028фейк", "ок\u200e"],
)
async def test_text_with_invisible_characters_is_refused_before_asking(pushed, text):
    asked: list[str] = []

    result = await call("legacy", answering("accept", True, asked), chat_id=GROUP, text=text)

    assert result.is_error
    assert "cannot see" in result.content[0].text
    assert asked == []
    assert pushed == []


@pytest.mark.anyio
async def test_private_chat_is_labelled_apart_from_a_group_named_after_the_person(pushed):
    asked: list[str] = []

    await call("legacy", answering("decline", None, asked), chat_id=PRIVATE, text="отчёт")
    await call("legacy", answering("decline", None, asked), chat_id=NAMESAKE, text="отчёт")

    assert asked[0].splitlines()[:2] == [
        "Отправить от вашего имени в личный чат с «Пётр Иванов»?",
        "⚠ У вас есть другой чат с таким же названием.",
    ]
    assert asked[1].splitlines()[0] == "Отправить от вашего имени в группу «Пётр Иванов» (участников: 3)?"


@pytest.mark.anyio
async def test_mixed_script_chat_name_is_flagged(pushed):
    asked: list[str] = []

    await call("legacy", answering("decline", None, asked), chat_id=HOMOGLYPH, text="отчёт")

    assert "смешаны латиница и кириллица" in asked[0].splitlines()[1]


@pytest.mark.anyio
async def test_thread_id_is_posted_in_its_canonical_form(pushed):
    await call("legacy", answering("accept", True), chat_id="100/0/group_01790000000000000", text="ок")

    assert pushed[0]["ChatId"] == THREAD


@pytest.mark.anyio
async def test_tools_declare_whether_they_write():
    async with Client(server.mcp) as client:
        tools = {t.name: t.annotations for t in (await client.list_tools()).tools}

    assert tools.pop("message_send").destructive_hint is True
    assert all(annotations.read_only_hint for annotations in tools.values())


def test_cookie_file_open_to_others_is_refused(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("TELEMOST_COOKIE=Session_id=x\n")
    env.chmod(0o644)
    monkeypatch.setattr(telemost_client, "ENV_FILE", env)

    with pytest.raises(telemost_client.TelemostError, match="chmod 600"):
        telemost_client.cookie_header()

    env.chmod(0o600)
    assert telemost_client.cookie_header() == "Session_id=x"
