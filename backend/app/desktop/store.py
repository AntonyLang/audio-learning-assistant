from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path


def now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, data: Path):
        self.path = data / "library.sqlite3"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS entries (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, instructions TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued', stage TEXT NOT NULL DEFAULT '等待处理',
                    target_seconds INTEGER NOT NULL, web_enabled INTEGER NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '', script TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '', audio_path TEXT NOT NULL DEFAULT '',
                    duration_seconds REAL NOT NULL DEFAULT 0,
                    segment_done INTEGER NOT NULL DEFAULT 0, segment_total INTEGER NOT NULL DEFAULT 0,
                    source_ids TEXT NOT NULL DEFAULT '[]', summary TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS sources (
                    id TEXT PRIMARY KEY, entry_id TEXT NOT NULL REFERENCES entries(id),
                    name TEXT NOT NULL, kind TEXT NOT NULL, location TEXT NOT NULL,
                    text TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
                    error TEXT NOT NULL DEFAULT '', external INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS source_entry ON sources(entry_id);
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, entry_id TEXT NOT NULL,
                    created_at TEXT NOT NULL, message TEXT NOT NULL
                );
            """)
            db.execute("""UPDATE entries SET status='interrupted', stage='上次运行中断，可继续',
                          error='应用已关闭；点击继续将复用已解析资料、讲稿和语音片段。'
                          WHERE status IN ('parsing','researching','voicing')""")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, title: str, instructions: str, seconds: int, web: bool) -> str:
        entry_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("""INSERT INTO entries
                (id,title,instructions,target_seconds,web_enabled,created_at,updated_at,status)
                VALUES (?,?,?,?,?,?,?,'collecting')""", (entry_id, title, instructions, seconds, web, now(), now()))
        return entry_id

    def add_source(self, entry_id: str, name: str, kind: str, location: str,
                   *, text: str = "", external: bool = False) -> str:
        source_id = uuid.uuid4().hex[:12]
        with self.connect() as db:
            db.execute("""INSERT INTO sources
                (id,entry_id,name,kind,location,text,status,external) VALUES (?,?,?,?,?,?,?,?)""",
                (source_id, entry_id, name, kind, location, text, "ready" if text else "pending", int(external)))
        return source_id

    def update_source(self, source_id: str, *, text: str = "", error: str = "") -> None:
        with self.connect() as db:
            db.execute("UPDATE sources SET text=?,error=?,status=? WHERE id=?",
                       (text, error, "error" if error else "ready", source_id))

    def update(self, entry_id: str, **values) -> None:
        allowed = {"title", "status", "stage", "error", "script", "model", "audio_path",
                   "duration_seconds", "segment_done", "segment_total", "source_ids", "summary"}
        if not values.keys() <= allowed:
            raise ValueError("Unknown entry field")
        values["updated_at"] = now()
        with self.connect() as db:
            db.execute(f"UPDATE entries SET {','.join(key + '=?' for key in values)} WHERE id=?",
                       (*values.values(), entry_id))

    def event(self, entry_id: str, message: str) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO events(entry_id,created_at,message) VALUES (?,?,?)",
                       (entry_id, now(), message))

    def get(self, entry_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
            return dict(row) if row else None

    def list(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("""SELECT e.*, (SELECT count(*) FROM sources s WHERE s.entry_id=e.id
                                 AND s.external=0) AS source_count FROM entries e ORDER BY created_at DESC""").fetchall()
            return [dict(row) for row in rows]

    def sources(self, entry_id: str) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM sources WHERE entry_id=? ORDER BY rowid", (entry_id,))]

    def events(self, entry_id: str) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM events WHERE entry_id=? ORDER BY seq", (entry_id,))]

    def queued(self) -> list[str]:
        with self.connect() as db:
            return [row[0] for row in db.execute("SELECT id FROM entries WHERE status='queued' ORDER BY created_at")]
