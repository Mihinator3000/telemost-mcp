"""Copy of synced messages with FTS5 search across all synced chats.

The copy lives in memory and is gone when the server exits. With a path it is a file instead:
readable by the owner only, kept out of Time Machine, and pruned to the retention period.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

SCHEMA = """
create table if not exists messages(
    chat_id text, ts_mcs integer, chat_name text, author text, body text, view text,
    primary key (chat_id, ts_mcs)
);
create virtual table if not exists messages_fts using fts5(body, author, content='messages');
create trigger if not exists messages_ai after insert on messages begin
    insert into messages_fts(rowid, body, author) values (new.rowid, new.body, new.author);
end;
create trigger if not exists messages_au after update on messages begin
    insert into messages_fts(messages_fts, rowid, body, author) values ('delete', old.rowid, old.body, old.author);
    insert into messages_fts(rowid, body, author) values (new.rowid, new.body, new.author);
end;
create trigger if not exists messages_ad after delete on messages begin
    insert into messages_fts(messages_fts, rowid, body, author) values ('delete', old.rowid, old.body, old.author);
end;
"""


def private_file(path: Path) -> None:
    """Create the file as owner-only before SQLite opens it; SQLite gives its journal the same mode."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.close(os.open(path, os.O_CREAT | os.O_RDONLY, 0o600))
    path.chmod(0o600)
    if shutil.which("tmutil"):
        subprocess.run(["tmutil", "addexclusion", str(path)], capture_output=True)


class Store:
    def __init__(self, path: Path | None = None, retention_days: int = 14):
        self.retention_days = retention_days if path else None
        if path:
            private_file(path)
        self.db = sqlite3.connect(path or ":memory:")
        self.db.executescript(SCHEMA)
        self.prune()

    def replace(self, chat_id: str, since_mcs: int, chat_name: str, messages: list[dict]) -> None:
        """Replace the chat's copy from `since_mcs` on, so messages deleted in Telemost leave it too."""
        rows = [
            (
                m["chat_id"], m["ts_mcs"], chat_name, m.get("author"),
                "\n".join([m.get("text", ""), *m.get("files", []), *m.get("quotes", []), *m.get("forwarded", [])]),
                json.dumps(m, ensure_ascii=False),
            )
            for m in messages if "chat_id" in m
        ]
        with self.db:
            self.db.execute("delete from messages where chat_id = ? and ts_mcs >= ?", (chat_id, since_mcs))
            self.db.executemany(
                """insert into messages values (?, ?, ?, ?, ?, ?)
                   on conflict (chat_id, ts_mcs) do update set
                   chat_name = excluded.chat_name, author = excluded.author, body = excluded.body, view = excluded.view""",
                rows,
            )

    def prune(self) -> None:
        if self.retention_days is None:
            return
        cutoff = int((time.time() - self.retention_days * 86400) * 1e6)
        with self.db:
            self.db.execute("delete from messages where ts_mcs < ?", (cutoff,))

    def search(self, query: str, chat_id: str | None, limit: int) -> list[dict]:
        # Each word becomes a quoted prefix term, so FTS syntax in the query cannot break the match.
        match = " ".join('"{}"*'.format(word.replace('"', "")) for word in query.split())
        sql = """select m.chat_name, m.view from messages_fts f join messages m on m.rowid = f.rowid
                 where messages_fts match ? {} order by m.ts_mcs desc limit ?"""
        args = [match, chat_id, limit] if chat_id else [match, limit]
        rows = self.db.execute(sql.format("and m.chat_id = ?" if chat_id else ""), args)
        return [{"chat": name, **json.loads(view)} if name else json.loads(view) for name, view in rows]

    def stats(self) -> dict:
        count, chats, oldest = self.db.execute(
            "select count(*), count(distinct chat_id), min(ts_mcs) from messages"
        ).fetchone()
        return {"messages": count, "chats": chats, "oldest_ts_mcs": oldest}
