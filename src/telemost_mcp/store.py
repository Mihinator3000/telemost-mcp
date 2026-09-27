"""Local SQLite copy of synced messages. FTS5 gives search across all synced chats."""

import json
import sqlite3
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
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def save(self, chat_name: str | None, messages: list[dict]) -> None:
        rows = [
            (
                m["chat_id"], m["ts_mcs"], chat_name, m.get("author"),
                "\n".join([m.get("text", ""), *m.get("files", []), *m.get("quotes", []), *m.get("forwarded", [])]),
                json.dumps(m, ensure_ascii=False),
            )
            for m in messages if "chat_id" in m
        ]
        with self.db:
            self.db.executemany(
                """insert into messages values (?, ?, ?, ?, ?, ?)
                   on conflict (chat_id, ts_mcs) do update set
                   chat_name = coalesce(excluded.chat_name, chat_name), author = excluded.author, body = excluded.body, view = excluded.view""",
                rows,
            )

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
