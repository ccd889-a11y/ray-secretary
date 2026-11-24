"""LINE webhook, asynchronous inbox and notification worker."""
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import signal
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from bot import HELP, application, enqueue, report_message
from domain import TZ
from line_api import Line, quick
from report import week_range
from storage import Store

LOGGER = logging.getLogger("ray-secretary")
MAX_WEBHOOK = 512 * 1024

@dataclass(frozen=True)
class Config:
    secret: str
    access_token: str
    pair_code: str
    public_url: str
    database: str
    weekly_push: bool

    @classmethod
    def from_env(cls):
        public_url = os.getenv("PUBLIC_BASE_URL", "").strip()
        if not public_url:
            domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
            if domain:
                public_url = f"https://{domain}"
        if not re.fullmatch(r"https://[A-Za-z0-9.-]+(?::\d+)?/?", public_url):
            raise ValueError("Set PUBLIC_BASE_URL to the public HTTPS origin, without /webhook.")
        secret = os.getenv("LINE_CHANNEL_SECRET", "").strip()
        token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
        code = os.getenv("PAIR_CODE", "").strip()
        if not secret or not token or not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", code):
            raise ValueError("Set LINE credentials and a new random PAIR_CODE (at least 16 characters).")
        database = os.getenv("DB_PATH", "/data/ray.sqlite3")
        if os.getenv("RAILWAY_SERVICE_ID"):
            mount = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
            if not mount or not Path(database).resolve().is_relative_to(Path(mount).resolve()):
                raise ValueError("Attach a Railway volume at /data and place DB_PATH within it.")
        return cls(secret, token, code, public_url.rstrip("/"), database, os.getenv("WEEKLY_PUSH", "1") == "1")

class Service:
    def __init__(self, config, line=None):
        self.config = config
        self.store = Store(config.database)
        self.line = line or Line(config.access_token)
        self.wake = threading.Event()
        self.stop = threading.Event()

    def accept(self, raw, signature, now):
        expected = base64.b64encode(hmac.digest(self.config.secret.encode(), raw, hashlib.sha256)).decode()
        if not signature or not hmac.compare_digest(expected.encode(), signature.encode()):
            return 401
        try:
            events = json.loads(raw)["events"]
        except (ValueError, KeyError, TypeError):
            return 400
        if not isinstance(events, list):
            return 400
        with self.store.connection() as db:
            for event in events:
                if not isinstance(event, dict):
                    continue
                source = event.get("source") or {}
                kind = event.get("type")
                if source.get("type") != "user" or not source.get("userId") or kind not in ("message", "follow", "unfollow"):
                    continue
                if kind == "message" and (event.get("message") or {}).get("type") != "text":
                    continue
                key = event.get("webhookEventId") or hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()
                clean = {"type": kind, "user_id": source["userId"], "reply": event.get("replyToken"),
                         "mode": event.get("mode", "active"),
                         "text": (event.get("message") or {}).get("text", "")}
                db.execute("INSERT OR IGNORE INTO inbox(id,event,received) VALUES(?,?,?)",
                           (key, json.dumps(clean, ensure_ascii=False), int(now.timestamp())))
        self.wake.set()
        return 200

    def message(self, db, event, now):
        user = event["user_id"]
        owner = db.execute("SELECT * FROM owner WHERE id=1").fetchone()
        kind = event["type"]
        if kind == "unfollow":
            db.execute("UPDATE owner SET active=0 WHERE id=1 AND user_id=?", (user,))
            return None
        if event["mode"] != "active":
            return None
        if owner and owner["user_id"] != user:
            return quick("這是私人工作祕書。只有已配對的 LINE 帳號可以使用。")
        if not owner:
            text = event["text"].strip()
            if text.startswith("配對 ") and hmac.compare_digest(text[3:].strip().encode(), self.config.pair_code.encode()):
                db.execute("INSERT INTO owner VALUES(1,?,1)", (user,))
                return quick("已完成私人配對。\n\n" + HELP)
            return quick("Ray 行動祕書尚待配對。請在這個一對一聊天室輸入「配對 ＋ 你的配對碼」。")
        db.execute("UPDATE owner SET active=1 WHERE id=1")
        if kind == "follow":
            return quick(HELP)
        if event["text"].strip().startswith("配對 "):
            return quick("此 LINE 帳號已配對。")
        return application(db, event["text"], now, self.config.public_url)

    def drain_inbox(self, now, limit=15):
        for _ in range(limit):
            with self.store.connection() as db:
                item = db.execute("SELECT * FROM inbox WHERE event IS NOT NULL ORDER BY rowid LIMIT 1").fetchone()
                if not item:
                    break
                event = json.loads(item["event"])
                # Use the receipt time to resolve relative dates, even if the queue was delayed.
                received = datetime.fromtimestamp(item["received"], TZ)
                response = self.message(db, event, received)
                db.execute("UPDATE inbox SET event=NULL,response=? WHERE id=?", (
                    json.dumps({"token": event["reply"], "message": response}, ensure_ascii=False)
                    if response and event["reply"] and int(now.timestamp()) - item["received"] < 50 else None, item["id"]))
            self.deliver_reply(item["id"])

    def deliver_reply(self, event_id):
        with self.store.connection() as db:
            item = db.execute("SELECT response FROM inbox WHERE id=?", (event_id,)).fetchone()
        if not item or not item["response"]:
            return
        db_response = json.loads(item["response"])
        status = self.line.send("reply", {"replyToken": db_response["token"], "messages": [db_response["message"]]})
        if status == 0 or status >= 500:
            return
        if not 200 <= status < 300:
            LOGGER.warning("LINE reply returned HTTP %s", status)
        with self.store.connection() as db:
            db.execute("UPDATE inbox SET response=NULL WHERE id=?", (event_id,))

    def tick(self, now):
        current = int(now.timestamp())
        self.drain_inbox(now)
        with self.store.connection() as db:
            db.execute("UPDATE inbox SET response=NULL WHERE response IS NOT NULL AND received<?", (current-50,))
            db.execute("DELETE FROM downloads WHERE expires<=?", (current,))
            db.execute("DELETE FROM inbox WHERE received<? AND event IS NULL AND response IS NULL", (current-30*86400,))
            owner = db.execute("SELECT user_id FROM owner WHERE active=1").fetchone()
            replies = db.execute("SELECT id FROM inbox WHERE response IS NOT NULL ORDER BY rowid LIMIT 5").fetchall()
            if self.config.weekly_push and owner and now.weekday() == 0 and 9 <= now.hour < 12:
                monday, _ = week_range(now)
                key = str(uuid.uuid5(uuid.NAMESPACE_URL, f"ray-weekly:{owner[0]}:{monday.isoformat()}"))
                due = int(now.replace(hour=9, minute=0, second=0, microsecond=0).timestamp())
                enqueue(db, "weekly", due, key=key)
        for item in replies:
            self.deliver_reply(item["id"])
        if owner:
            self.push(owner[0], now)

    def push(self, user, now):
        current = int(now.timestamp())
        with self.store.connection() as db:
            db.execute("UPDATE outbox SET state='expired' WHERE state='pending' AND due<=?", (current-23*3600,))
            job = db.execute("SELECT * FROM outbox WHERE state='pending' AND next_attempt<=? ORDER BY due LIMIT 1", (current,)).fetchone()
            if not job:
                return
            if job["kind"] == "weekly":
                due = datetime.fromtimestamp(job["due"], TZ)
                message = report_message(db, due, self.config.public_url)
            else:
                row = db.execute("SELECT * FROM records WHERE id=? AND status='open'", (job["record_id"],)).fetchone()
                if not row:
                    db.execute("UPDATE outbox SET state='cancelled' WHERE key=?", (job["key"],))
                    return
                record = f'#{row["id"]}'
                when = datetime.fromtimestamp(job["due"], TZ).strftime("%m/%d %H:%M")
                prefix = "待辦提醒" if row["kind"] == "task" else "行程提醒"
                buttons = [("看待辦", "待辦追蹤")] if row["kind"] == "task" else [("看本週行程", "查行程 本週")]
                if row["kind"] == "task":
                    buttons.insert(0, ("標記完成", f"完成 {row['id']}"))
                message = quick(f'{when} {prefix}\n{record}｜{row["title"]}', *buttons)
        status = self.line.send("push", {"to": user, "messages": [message]}, job["key"])
        attempts = job["attempts"] + 1
        state = "sent" if 200 <= status < 300 or status == 409 else "failed"
        if attempts < 6 and (status == 0 or status == 429 or status >= 500):
            state = "pending"
        if state == "failed":
            LOGGER.warning("LINE push stopped after HTTP %s", status)
        with self.store.connection() as db:
            db.execute("UPDATE outbox SET state=?,attempts=?,next_attempt=? WHERE key=?",
                       (state, attempts, current + min(900, 15 * 2**attempts), job["key"]))
        if state != "pending":
            self.wake.set()

    def run(self):
        while not self.stop.is_set():
            self.wake.clear()
            try:
                self.tick(datetime.now(TZ))
            except Exception:
                LOGGER.exception("Background worker failed to process a queue item")
            self.wake.wait(3)

    def download(self, token, now):
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            return None
        with self.store.connection() as db:
            row = db.execute("SELECT data FROM downloads WHERE token=? AND expires>?", (token, int(now.timestamp()))).fetchone()
        return row[0] if row else None

def handler(service):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(12)

        def log_message(self, *args):
            # Do not log private report bearer URLs.
            return

        def result(self, status, body=b"", content_type="text/plain; charset=utf-8", attachment=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if attachment:
                self.send_header("Content-Disposition", f'attachment; filename="{attachment}"')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path in ("/", "/healthz"):
                self.result(200, b"ok\n")
                return
            if path.startswith("/report/"):
                file = service.download(path[8:], datetime.now(TZ))
                if file:
                    self.result(200, file, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "Ray-Weekly-Report.xlsx")
                    return
                self.result(404, "下載連結無效或已到期，請從 LINE 再點一次看週報。".encode())
                return
            self.result(404)

        def do_POST(self):
            if urlsplit(self.path).path != "/webhook":
                self.result(404)
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
                if not 0 < length <= MAX_WEBHOOK:
                    self.result(413)
                    return
                body = self.rfile.read(length)
            except (ValueError, TimeoutError):
                self.result(400)
                return
            status = service.accept(body, self.headers.get("X-Line-Signature"), datetime.now(TZ))
            self.result(status, b"ok\n" if status == 200 else b"")
    return Handler

def main():
    logging.basicConfig(level=logging.INFO)
    service = Service(Config.from_env())
    http = ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "8080"))), handler(service))
    worker = threading.Thread(target=service.run, daemon=True)
    worker.start()
    def shutdown(*_):
        service.stop.set()
        service.wake.set()
        threading.Thread(target=http.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    LOGGER.info("Ray secretary listening on port %s", http.server_port)
    with http:
        http.serve_forever()
    worker.join(timeout=8)

if __name__ == "__main__":
    main()
