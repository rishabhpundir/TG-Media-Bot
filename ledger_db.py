"""
SQLite-backed ledger for TG-Media-Bot.

Replaces two JSON files with one database:
  * ledger.json          -> `uploads` table   (Google Drive uploads)
  * download_ledger.json -> `downloads` table (Telegram downloads)

Why this exists
---------------
The JSON ledger did a full 4MB parse + full indented re-serialise on EVERY
file and EVERY folder touched during an upload. A 50-episode season meant
~100 full-file rewrites. It was also not atomic: two concurrent uploads
running in separate asyncio.to_thread workers would both load, both mutate
and both save -- last writer wins, the other's entries silently vanished.

Key design decisions
--------------------
1. Drive uploads are keyed on (absolute local path, Drive parent folder id).
   The old scheme keyed on a path relative to a mutable `BASE_DIR` global,
   which meant tv/ShowA/S01E01.mkv and tv/ShowB/S01E01.mkv collided on the
   literal key "S01E01.mkv" -- the second upload was silently skipped.
   Absolute paths kill that bug and make BASE_DIR unnecessary.

2. The parent folder id is part of the key, so the same local file CAN be
   recorded against two different Drive folders without conflict. This is
   what makes the movies/tv bifurcation safe without any root-namespacing
   hack.

3. WAL journalling + a 30s busy timeout. Concurrent writers queue instead
   of clobbering. One connection per thread via threading.local.

4. No ORM, no dependencies -- sqlite3 is stdlib.
"""

import os
import json
import sqlite3
import logging
import threading
from datetime import datetime

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.realpath(__file__))
DB_PATH = os.getenv("LEDGER_DB_PATH", os.path.join(PROJECT_ROOT, "ledger.db"))

_local = threading.local()
_schema_lock = threading.Lock()
_schema_ready = False


SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
    path          TEXT    NOT NULL,          -- absolute local path
    parent_id     TEXT    NOT NULL,          -- Drive folder it was uploaded INTO
    gid           TEXT    NOT NULL,          -- Drive file/folder id
    name          TEXT    NOT NULL,          -- basename, for human lookups
    is_folder     INTEGER NOT NULL DEFAULT 0,
    size_bytes    INTEGER,                   -- NULL for folders
    uploaded_at   TEXT    NOT NULL,
    source        TEXT    NOT NULL DEFAULT 'upload',  -- upload | rebuild | import
    PRIMARY KEY (path, parent_id)
);

CREATE INDEX IF NOT EXISTS idx_uploads_name   ON uploads(name);
CREATE INDEX IF NOT EXISTS idx_uploads_gid    ON uploads(gid);
CREATE INDEX IF NOT EXISTS idx_uploads_parent ON uploads(parent_id);

CREATE TABLE IF NOT EXISTS downloads (
    file_uid          TEXT PRIMARY KEY,      -- "doc:<id>" / "photo:<id>"
    filename          TEXT,
    path              TEXT,
    size              INTEGER,
    downloaded_at     TEXT,
    channel_id        INTEGER,
    message_id        INTEGER,
    drive_folder      TEXT,                  -- set once auto-uploaded
    drive_uploaded_at TEXT,
    extra             TEXT                   -- JSON blob, forward-compat
);

CREATE INDEX IF NOT EXISTS idx_downloads_path ON downloads(path);
CREATE INDEX IF NOT EXISTS idx_downloads_name ON downloads(filename);
"""

# Columns the downloads table stores natively; anything else in an entry dict
# gets tucked into `extra` so a future field never silently disappears.
_DOWNLOAD_COLS = (
    "filename", "path", "size", "downloaded_at",
    "channel_id", "message_id", "drive_folder", "drive_uploaded_at",
)


# ----------------------------------------------------------------------------
# Connection handling
# ----------------------------------------------------------------------------

def _ensure_schema(conn):
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        conn.executescript(SCHEMA)
        conn.commit()
        _schema_ready = True


def connect():
    """Thread-local connection. Safe to call from asyncio.to_thread workers."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        _local.conn = conn
    _ensure_schema(conn)
    return conn


def close():
    """Close this thread's connection. Only needed in scripts."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


# ----------------------------------------------------------------------------
# uploads  (Google Drive)
# ----------------------------------------------------------------------------

def get_upload(path, parent_id):
    """Drive id if this exact path was already uploaded into this folder, else None."""
    row = connect().execute(
        "SELECT gid FROM uploads WHERE path = ? AND parent_id = ?",
        (os.path.abspath(path), str(parent_id)),
    ).fetchone()
    return row["gid"] if row else None


def put_upload(path, parent_id, gid, is_folder=False, size_bytes=None,
               name=None, source="upload"):
    """Record (or refresh) an upload. Idempotent."""
    path = os.path.abspath(path)
    conn = connect()
    conn.execute(
        """INSERT OR REPLACE INTO uploads
           (path, parent_id, gid, name, is_folder, size_bytes, uploaded_at, source)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            path,
            str(parent_id),
            str(gid),
            name or os.path.basename(path),
            1 if is_folder else 0,
            size_bytes,
            datetime.now().isoformat(timespec="seconds"),
            source,
        ),
    )
    conn.commit()


def put_uploads_bulk(rows):
    """Insert many upload rows in ONE transaction. Used by the rebuild script.

    `rows` is an iterable of dicts with keys matching put_upload's parameters.
    """
    conn = connect()
    now = datetime.now().isoformat(timespec="seconds")
    payload = [
        (
            os.path.abspath(r["path"]),
            str(r["parent_id"]),
            str(r["gid"]),
            r.get("name") or os.path.basename(r["path"]),
            1 if r.get("is_folder") else 0,
            r.get("size_bytes"),
            r.get("uploaded_at") or now,
            r.get("source", "rebuild"),
        )
        for r in rows
    ]
    conn.executemany(
        """INSERT OR REPLACE INTO uploads
           (path, parent_id, gid, name, is_folder, size_bytes, uploaded_at, source)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        payload,
    )
    conn.commit()
    return len(payload)


def forget_upload(path, parent_id=None):
    """Drop a path from the ledger so the next /gd re-checks Drive for it."""
    conn = connect()
    path = os.path.abspath(path)
    if parent_id:
        cur = conn.execute(
            "DELETE FROM uploads WHERE path = ? AND parent_id = ?", (path, str(parent_id))
        )
    else:
        cur = conn.execute("DELETE FROM uploads WHERE path = ?", (path,))
    conn.commit()
    return cur.rowcount


def find_uploads(keyword, limit=50):
    """Substring search on basename. For the inspection you lost with JSON."""
    return connect().execute(
        "SELECT * FROM uploads WHERE name LIKE ? ORDER BY uploaded_at DESC LIMIT ?",
        (f"%{keyword}%", limit),
    ).fetchall()


# ----------------------------------------------------------------------------
# downloads  (Telegram)
# ----------------------------------------------------------------------------

def get_download(file_uid):
    """Return a plain dict shaped like the old JSON entry, or None."""
    row = connect().execute(
        "SELECT * FROM downloads WHERE file_uid = ?", (file_uid,)
    ).fetchone()
    if not row:
        return None

    entry = {c: row[c] for c in _DOWNLOAD_COLS if row[c] is not None}
    if row["extra"]:
        try:
            entry.update(json.loads(row["extra"]))
        except (json.JSONDecodeError, TypeError):
            pass
    return entry


def put_download(file_uid, entry):
    """Upsert a download record. `entry` is the same dict shape as before."""
    entry = dict(entry or {})
    known = {c: entry.pop(c, None) for c in _DOWNLOAD_COLS}
    extra = json.dumps(entry) if entry else None

    conn = connect()
    conn.execute(
        """INSERT OR REPLACE INTO downloads
           (file_uid, filename, path, size, downloaded_at,
            channel_id, message_id, drive_folder, drive_uploaded_at, extra)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            file_uid,
            known["filename"], known["path"], known["size"], known["downloaded_at"],
            known["channel_id"], known["message_id"],
            known["drive_folder"], known["drive_uploaded_at"],
            extra,
        ),
    )
    conn.commit()


def find_downloads(keyword, limit=50):
    return connect().execute(
        "SELECT * FROM downloads WHERE filename LIKE ? ORDER BY downloaded_at DESC LIMIT ?",
        (f"%{keyword}%", limit),
    ).fetchall()


# ----------------------------------------------------------------------------
# Maintenance
# ----------------------------------------------------------------------------

def stats():
    conn = connect()
    out = {
        "db_path": DB_PATH,
        "db_size_bytes": os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0,
        "uploads": conn.execute("SELECT COUNT(*) c FROM uploads").fetchone()["c"],
        "upload_files": conn.execute(
            "SELECT COUNT(*) c FROM uploads WHERE is_folder = 0").fetchone()["c"],
        "upload_folders": conn.execute(
            "SELECT COUNT(*) c FROM uploads WHERE is_folder = 1").fetchone()["c"],
        "downloads": conn.execute("SELECT COUNT(*) c FROM downloads").fetchone()["c"],
    }
    out["by_parent"] = {
        r["parent_id"]: r["c"]
        for r in conn.execute(
            "SELECT parent_id, COUNT(*) c FROM uploads GROUP BY parent_id")
    }
    return out


def vacuum():
    """Reclaim space after large deletions. Rarely needed."""
    conn = connect()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("VACUUM")
    conn.commit()


if __name__ == "__main__":
    import pprint
    logging.basicConfig(level=logging.INFO)
    pprint.pprint(stats())
