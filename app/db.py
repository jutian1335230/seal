"""SQLite storage. One firm, one database file, no external server."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    client_name TEXT NOT NULL,
    client_email TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    note TEXT NOT NULL DEFAULT '',
    original_name TEXT NOT NULL DEFAULT '',
    doc_sha256 TEXT NOT NULL DEFAULT '',
    rotated INTEGER NOT NULL DEFAULT 0,
    accepting INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS signers (
    id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'client',
    token TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending',
    signature_mode TEXT,
    ip TEXT,
    user_agent TEXT,
    created_at TEXT NOT NULL,
    viewed_at TEXT,
    signed_at TEXT
);

CREATE TABLE IF NOT EXISTS fields (
    id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    signer_id TEXT NOT NULL REFERENCES signers(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    page INTEGER NOT NULL,
    x REAL NOT NULL,
    y REAL NOT NULL,
    w REAL NOT NULL,
    h REAL NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    party TEXT NOT NULL DEFAULT 'client',
    source TEXT NOT NULL DEFAULT 'manual'
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    signer_id TEXT,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    ip TEXT,
    created_at TEXT NOT NULL
);
"""


def data_dir() -> Path:
    path = Path(os.environ.get("SEAL_DATA", ROOT / "data"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def pretty_time(value: str | None) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    return dt.strftime("%d %b %Y, %H:%M UTC")


@contextmanager
def connect():
    connection = sqlite3.connect(data_dir() / "seal.db")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db() -> None:
    with connect() as connection:
        connection.executescript(SCHEMA)


def _one(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row else None


def _all(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(row) for row in rows]


def setting(key: str, default: str = "") -> str:
    with connect() as connection:
        row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO settings(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )


def create_file(record: dict) -> None:
    columns = [
        "id",
        "title",
        "client_name",
        "client_email",
        "status",
        "note",
        "original_name",
        "doc_sha256",
        "rotated",
        "accepting",
        "created_at",
    ]
    with connect() as connection:
        connection.execute(
            f"INSERT INTO files ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            [record[column] for column in columns],
        )


def list_files() -> list[dict]:
    with connect() as connection:
        rows = connection.execute("SELECT * FROM files ORDER BY created_at DESC").fetchall()
    return _all(rows)


def get_file(file_id: str) -> dict | None:
    with connect() as connection:
        row = connection.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    return _one(row)


def update_file(file_id: str, **columns: object) -> None:
    allowed = {
        "title",
        "client_name",
        "client_email",
        "status",
        "note",
        "sent_at",
        "completed_at",
        "accepting",
        "rotated",
    }
    keys = [key for key in columns if key in allowed]
    if not keys:
        return
    assignments = ", ".join(f"{key} = ?" for key in keys)
    with connect() as connection:
        connection.execute(
            f"UPDATE files SET {assignments} WHERE id = ?",
            [columns[key] for key in keys] + [file_id],
        )


def delete_file(file_id: str) -> None:
    with connect() as connection:
        connection.execute("DELETE FROM files WHERE id = ?", (file_id,))


def create_signer(record: dict) -> None:
    columns = ["id", "file_id", "name", "email", "role", "token", "status", "created_at"]
    with connect() as connection:
        connection.execute(
            f"INSERT INTO signers ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            [record[column] for column in columns],
        )


def list_signers(file_id: str) -> list[dict]:
    with connect() as connection:
        rows = connection.execute(
            "SELECT * FROM signers WHERE file_id = ? ORDER BY created_at",
            (file_id,),
        ).fetchall()
    return _all(rows)


def get_signer(signer_id: str) -> dict | None:
    with connect() as connection:
        row = connection.execute("SELECT * FROM signers WHERE id = ?", (signer_id,)).fetchone()
    return _one(row)


def get_signer_by_token(token: str) -> dict | None:
    with connect() as connection:
        row = connection.execute("SELECT * FROM signers WHERE token = ?", (token,)).fetchone()
    return _one(row)


def update_signer(signer_id: str, **columns: object) -> None:
    allowed = {"name", "email", "role", "status", "signature_mode", "ip", "user_agent", "viewed_at", "signed_at"}
    keys = [key for key in columns if key in allowed]
    if not keys:
        return
    assignments = ", ".join(f"{key} = ?" for key in keys)
    with connect() as connection:
        connection.execute(
            f"UPDATE signers SET {assignments} WHERE id = ?",
            [columns[key] for key in keys] + [signer_id],
        )


def delete_signer(signer_id: str) -> None:
    with connect() as connection:
        connection.execute("DELETE FROM signers WHERE id = ?", (signer_id,))


def create_field(record: dict) -> None:
    columns = [
        "id",
        "file_id",
        "signer_id",
        "kind",
        "page",
        "x",
        "y",
        "w",
        "h",
        "label",
        "party",
        "source",
    ]
    with connect() as connection:
        connection.execute(
            f"INSERT INTO fields ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            [record[column] for column in columns],
        )


def list_fields(file_id: str) -> list[dict]:
    with connect() as connection:
        rows = connection.execute(
            "SELECT * FROM fields WHERE file_id = ? ORDER BY page, y, x",
            (file_id,),
        ).fetchall()
    return _all(rows)


def fields_for_signer(signer_id: str) -> list[dict]:
    with connect() as connection:
        rows = connection.execute(
            "SELECT * FROM fields WHERE signer_id = ? ORDER BY page, y, x",
            (signer_id,),
        ).fetchall()
    return _all(rows)


def get_field(field_id: str) -> dict | None:
    with connect() as connection:
        row = connection.execute("SELECT * FROM fields WHERE id = ?", (field_id,)).fetchone()
    return _one(row)


def update_field(field_id: str, **columns: object) -> None:
    allowed = {"signer_id", "x", "y", "w", "h", "page"}
    keys = [key for key in columns if key in allowed]
    if not keys:
        return
    assignments = ", ".join(f"{key} = ?" for key in keys)
    with connect() as connection:
        connection.execute(
            f"UPDATE fields SET {assignments} WHERE id = ?",
            [columns[key] for key in keys] + [field_id],
        )


def delete_field(field_id: str) -> None:
    with connect() as connection:
        connection.execute("DELETE FROM fields WHERE id = ?", (field_id,))


def add_event(file_id: str, kind: str, detail: str = "", signer_id: str | None = None, ip: str | None = None) -> None:
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO events (file_id, signer_id, kind, detail, ip, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (file_id, signer_id, kind, detail, ip, utcnow()),
        )


def list_events(file_id: str) -> list[dict]:
    with connect() as connection:
        rows = connection.execute(
            "SELECT * FROM events WHERE file_id = ? ORDER BY id",
            (file_id,),
        ).fetchall()
    return _all(rows)
