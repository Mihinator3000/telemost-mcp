"""Client for the private API of the Telemost web client (telemost.360.yandex.ru).

Two channels share one Yandex session. The user pastes its Cookie header into the env file:
- HTTP `POST messenger.360.yandex.ru/api/` takes the form field `request={"method", "params"}`.
- The Xiva WebSocket at push.yandex.ru carries `whoami`, `history` and other fanout calls.
  A frame is msgpack `1, [service_index, req_id, method]`, a 12-byte header, then JSON.
The protocol is reverse-engineered from web client 211.3.0. Yandex can change it without notice.
"""

import asyncio
import itertools
import json
import logging
import os
import re
import secrets
import string
import uuid
from pathlib import Path
from urllib.parse import urlencode

import httpx
import msgpack
import websockets

ORIGIN = "https://telemost.360.yandex.ru"
API_URL = "https://messenger.360.yandex.ru/api/"
XIVA_URL = "wss://push.yandex.ru/v2/subscribe/websocket"
XIVA_SERVICE = "messenger-prod:version5*common+version5*main"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
ORIGIN_SERVICE_ID = 27
API_HEADERS = {
    "X-Application-Id": "Yamb-web",
    "X-Origin-Service-ID": str(ORIGIN_SERVICE_ID),
    "X-Version": "5",
    "Origin": ORIGIN,
    "Referer": ORIGIN + "/",
    "User-Agent": USER_AGENT,
}
# The web client writes 5 into byte 0 of this header and leaves the rest empty.
PAYLOAD_HEADER = b"\x05" + bytes(11)
DATA, PROXY_STATUS = 1, 2
# `push` envelope fields as the Telemost web client 212.6.0 sends them.
PUSH_USER_AGENT = "telemost-web/212.6.0"
# A posted message comes back FULLY_COMMITTED; a repeated PayloadId comes back DUPLICATE and posts nothing new.
PUSH_COMMITTED = {1: "FULLY_COMMITTED", 8: "DUPLICATE"}
PUSH_STATUSES = PUSH_COMMITTED | {
    0: "UNCOMMITTED", 2: "UNIPROXY_COMMITTED", 3: "FAILED", 4: "NO_SUCH_CHAT", 5: "NOT_LEADING",
    6: "FOREIGN_PARTITION", 7: "SENDER_NOT_IN_CHAT", 9: "POSTPROC_COMMITTED", 10: "KIKIMR_WRITE_FAILED",
    11: "MESSAGE_NOT_FOUND", 12: "DEQUEUED_AFTER_ERROR", 13: "BAD_REQUEST", 14: "FILESHARE_FAILED",
    15: "NO_PERMISSION", 16: "CONFLICT", 17: "NO_SUCH_USER", 18: "THROTTLED",
}


# websockets logs the handshake headers, the session Cookie included, at DEBUG.
logging.getLogger("websockets").setLevel(logging.INFO)


class TelemostError(RuntimeError):
    pass


ENV_FILE = Path(os.environ.get("TELEMOST_ENV", "~/.config/telemost-mcp/.env")).expanduser()


def cookie_header() -> str:
    """Read TELEMOST_COOKIE from the env file on every call, so a re-pasted cookie applies without a restart."""
    if ENV_FILE.exists() and ENV_FILE.stat().st_mode & 0o077:
        raise TelemostError(f"{ENV_FILE} is open to other users; restrict it with chmod 600 {ENV_FILE}")
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    values = dict(line.split("=", 1) for line in lines if line.startswith("TELEMOST_COOKIE="))
    cookie = values.get("TELEMOST_COOKIE", os.environ.get("TELEMOST_COOKIE", "")).strip().strip("'\"")
    if "Session_id=" not in cookie:
        raise TelemostError(
            f"No Yandex session cookie. Copy the Cookie header of a messenger.360.yandex.ru/api/ request "
            f"from DevTools into {ENV_FILE} as TELEMOST_COOKIE=..."
        )
    return cookie


def browser_id(cookie: str) -> str:
    """The `yandexuid` cookie: the web client logs it as YandexUid, and the backend rejects a push without it."""
    match = re.search(r"(?:^|;\s*)yandexuid=(\d+)", cookie)
    if match is None:
        raise TelemostError("The cookie has no yandexuid; copy the whole Cookie header again")
    return match.group(1)


class Xiva:
    """One Xiva WebSocket. It matches each response to its request by req_id."""

    def __init__(self, cookie: str, uid: int):
        self.cookie = cookie
        self.uid = uid
        self.pending: dict[int, asyncio.Future] = {}
        self.req_ids = itertools.count(1)
        self.subscription_id: str | None = None

    async def connect(self) -> None:
        session = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(19))
        query = urlencode(
            {"service": XIVA_SERVICE, "session": session, "client": "web_main", "user": self.uid}, safe="*"
        )
        self.ws = await websockets.connect(
            f"{XIVA_URL}?{query}",
            additional_headers={"Cookie": self.cookie, "Origin": ORIGIN, "User-Agent": USER_AGENT},
            max_size=None,
        )
        # The server confirms the subscription before it accepts requests; `push` must carry its id.
        async with asyncio.timeout(10):
            async for frame in self.ws:
                event = json.loads(frame) if isinstance(frame, str) else {}
                operation = event.get("operation")
                if operation == "subscribed":
                    self.subscription_id = event.get("subscription-id")
                    break
                if operation == "xivaws-error":
                    raise TelemostError(f"Xiva refused the connection: {frame}")
        self.reader = asyncio.create_task(self.read())

    @property
    def alive(self) -> bool:
        return not self.reader.done()

    async def read(self) -> None:
        try:
            async for frame in self.ws:
                if isinstance(frame, str):
                    continue  # Pings and service operations.
                unpacker = msgpack.Unpacker(raw=False)
                unpacker.feed(frame)
                kind, header = unpacker.unpack(), unpacker.unpack()
                if kind == DATA:
                    self.resolve(header[1], json.loads(frame[unpacker.tell() + len(PAYLOAD_HEADER):]))
                elif kind == PROXY_STATUS:
                    self.resolve(header[0], TelemostError(f"Xiva proxy error code {header[1]}"))
        finally:
            for req_id in list(self.pending):
                self.resolve(req_id, TelemostError("Xiva connection closed"))

    def resolve(self, req_id: int, result) -> None:
        future = self.pending.pop(req_id, None)
        if future is None or future.done():
            return
        if isinstance(result, Exception):
            future.set_exception(result)
        else:
            future.set_result(result)

    async def call(self, method: str, body: dict, timeout: float = 30) -> dict:
        req_id = next(self.req_ids)
        future = asyncio.get_running_loop().create_future()
        self.pending[req_id] = future
        payload = json.dumps({"RequestId": str(uuid.uuid4()), **body}).encode()
        await self.ws.send(msgpack.packb(DATA) + msgpack.packb([0, req_id, method]) + PAYLOAD_HEADER + payload)
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(req_id, None)


class Telemost:
    """Session facade: HTTP API calls and a lazily (re)connected Xiva socket."""

    def __init__(self):
        self.http = httpx.AsyncClient(headers=API_HEADERS, timeout=30)
        self.me: dict | None = None
        self.xiva: Xiva | None = None
        self.lock = asyncio.Lock()

    async def api(self, method: str, **params) -> dict:
        headers = {"X-Request-Id": str(uuid.uuid4()), "X-Request-Attempt": "0", "Cookie": cookie_header()}
        if self.me:
            headers |= {"X-Uid": str(self.me["uid"]), "X-Ya-Organization-Id": str(self.me["organization_id"])}
        request = json.dumps({"method": method, "params": params})
        response = await self.http.post(API_URL, files={"request": (None, request)}, headers=headers)
        response.raise_for_status()
        body = response.json()
        if body.get("status") != "ok":
            raise TelemostError(f"{method} failed: {body}")
        return body["data"]

    async def session(self) -> Xiva:
        async with self.lock:
            if self.xiva is None or not self.xiva.alive:
                await self.login()
        return self.xiva

    async def fanout(self, method: str, body: dict) -> dict:
        return await (await self.session()).call(method, body)

    async def push(self, plain: dict) -> dict:
        """Post one `ClientMessage.Plain`. It is never retried: a retry could post the message twice."""
        xiva = await self.session()
        if not xiva.subscription_id:
            raise TelemostError("Xiva sent no subscription id, so a message cannot be posted")
        body = {
            "ClientTransportId": {"XivaSubscriptionId": xiva.subscription_id},
            "UserAgent": PUSH_USER_AGENT,
            "ClientMessage": {"Plain": plain, "LogData": {"YandexUid": browser_id(xiva.cookie)}},
            "Meta": {"Origin": ORIGIN_SERVICE_ID},
            "ClientSupportedFeatures": 0,
        }
        try:
            result = await xiva.call("push", body)
        except TimeoutError:
            raise TelemostError("No answer to push: the message may or may not be posted, read the chat before resending")
        status = result.get("Status")
        if status not in PUSH_COMMITTED:
            raise TelemostError(f"push not committed: {PUSH_STATUSES.get(status, status)}")
        return result

    async def login(self) -> None:
        cookie = cookie_header()
        # Session_id lists the logged-in accounts as `|<uid>.`; the first one is the default account.
        self.xiva = Xiva(cookie, int(re.search(r"Session_id=[^;]*?\|(\d+)\.", cookie).group(1)))
        await self.xiva.connect()
        info = (await self.xiva.call("whoami", {}))["UserInfo"]
        organizations = info.get("OrganizationInfos") or [{}]
        self.me = {
            "uid": info["Uid"],
            "guid": info["Guid"],
            "name": info.get("DisplayName"),
            "email": info.get("Email"),
            "organization_id": organizations[0].get("OrganizationId", 0),
            "organization": organizations[0].get("OrganizationName"),
        }
