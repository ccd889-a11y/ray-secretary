"""Durable local storage for one LINE official account."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS owner (id INTEGER PRIMARY KEY CHECK(id=1), user_id TEXT NOT NULL, active INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS inbox (id TEXT PRIMARY KEY, event TEXT, received INTEGER NOT NULL, response TEXT);
CREATE TABLE IF NOT EXISTS records (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL, category INTEGER,
  starts INTEGER, ends INTEGER, all_day INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'open', created INTEGER NOT NULL, completed INTEGER, result TEXT
);
CREATE INDEX IF NOT EXISTS records_schedule ON records(kind, status, starts);
CREATE TABLE IF NOT EXISTS expenses (
  id INTEGER PRIMARY KEY, title TEXT NOT NULL, category TEXT NOT NULL, cents INTEGER NOT NULL, date TEXT NOT NULL, voided INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS expenses_date ON expenses(date);
CREATE TABLE IF NOT EXISTS outbox (
  key TEXT PRIMARY KEY, record_id INTEGER REFERENCES records(id), kind TEXT NOT NULL, due INTEGER NOT NULL,
  next_attempt INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS outbox_ready ON outbox(state, next_attempt);
CREATE TABLE IF NOT EXISTS downloads (token TEXT PRIMARY KEY, expires INTEGER NOT NULL, data BLOB NOT NULL);
"""
class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()
