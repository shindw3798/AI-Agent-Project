"""SQLite에 대화 이력과 대상 확인 대기 상태를 저장한다."""
import sqlite3
import json
import time
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "session.db"


class SqliteSessionStore:
    def __init__(self, db_path: Path = DB_PATH):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS messages ("
            " thread_id TEXT, role TEXT, content TEXT, created TEXT)"
        )
        self.conn.commit()
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS clarifications (thread_id TEXT PRIMARY KEY,"
            " clarification_id TEXT UNIQUE, payload TEXT, expires REAL, status TEXT)")
        self.conn.commit()

    def save_pending(self, thread_id, payload, ttl=1800):
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO clarifications VALUES (?,?,?,?,?)",
                              (thread_id, payload["clarification_id"], json.dumps(payload),
                               time.time()+ttl, "active"))

    def pending(self, thread_id):
        row = self.conn.execute(
            "SELECT payload,expires,status FROM clarifications WHERE thread_id=?", (thread_id,)).fetchone()
        return json.loads(row[0]) if row and row[1] > time.time() and row[2] == "active" else None

    def cancel_pending(self, thread_id):
        with self.conn:
            self.conn.execute("UPDATE clarifications SET status='cancelled' WHERE thread_id=?", (thread_id,))

    def consume_pending(self, thread_id, clarification_id):
        with self.conn:
            row = self.conn.execute(
                "SELECT payload FROM clarifications WHERE thread_id=? AND clarification_id=? "
                "AND expires>? AND status='active'", (thread_id, clarification_id, time.time())).fetchone()
            if not row:
                raise ValueError("INVALID_OR_EXPIRED_CLARIFICATION")
            self.conn.execute("UPDATE clarifications SET status='consumed' WHERE thread_id=?", (thread_id,))
            return json.loads(row[0])

    def load(self, thread_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT role, content FROM messages WHERE thread_id=? ORDER BY rowid",
            (thread_id,),
        ).fetchall()
        return [{"role": r, "content": c} for r, c in rows]

    def append(self, thread_id: str, role: str, content: str) -> None:
        self.conn.execute(
            "INSERT INTO messages VALUES (?,?,?,?)",
            (thread_id, role, content, datetime.now().isoformat()),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
