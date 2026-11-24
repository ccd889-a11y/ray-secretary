import base64
import hmac
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from openpyxl import load_workbook

from domain import TZ
from install_menu import LABELS, menu
from server import Config, Service, handler

class FakeLine:
    def __init__(self):
        self.sent = []
        self.push_codes = []

    def send(self, endpoint, data, retry_key=None):
        self.sent.append((endpoint, data, retry_key))
        return self.push_codes.pop(0) if endpoint == "push" and self.push_codes else 200

class ApplicationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.line = FakeLine()
        self.config = Config("test-secret", "test-token", "abcdefghijklmnop", "https://ray.example",
                             str(Path(self.tmp.name) / "ray.db"), True)
        self.app = Service(self.config, self.line)
        self.now = datetime(2026, 11, 20, 9, 0, tzinfo=TZ)
        self.sequence = 0
        self.send("配對 abcdefghijklmnop")

    def payload(self, text, user="Uowner", key=None):
        self.sequence += 1
        return json.dumps({"events": [{"type":"message","webhookEventId":key or str(self.sequence),
            "replyToken":str(self.sequence),"source":{"type":"user","userId":user},
            "message":{"type":"text","text":text}}]}, ensure_ascii=False).encode()

    def send(self, text, user="Uowner", key=None):
        raw = self.payload(text, user, key)
        self.assertEqual(self.app.accept(raw, self.signature(raw), self.now), 200)
        self.app.tick(self.now)
        return self.line.sent[-1][1]["messages"][0]

    def signature(self, raw):
        return base64.b64encode(hmac.digest(self.config.secret.encode(), raw, "sha256")).decode()

    def test_schedule_and_due_reminder_retry_then_done(self):
        self.assertIn("14:00", self.send("行程 明天 14:00-15:00 拜訪")["text"])
        self.assertIn("拜訪", self.send("查行程 明天")["text"])
        self.assertIn("資料", self.send("待辦 今天 09:01 準備資料")["text"])
        self.line.push_codes[:] = [503, 409]
        self.now += timedelta(minutes=1)
        self.app.tick(self.now)
        self.now += timedelta(seconds=31)
        self.app.tick(self.now)
        pushed = [call for call in self.line.sent if call[0] == "push"]
        self.assertEqual(len(pushed), 2)
        self.assertEqual(pushed[0][2], pushed[1][2])
        self.assertIn("準備資料", pushed[1][1]["messages"][0]["text"])
        self.assertIn("已完成", self.send("完成 2 已整理")["text"])
        self.assertNotIn("準備資料", self.send("待辦追蹤")["text"])

    def test_postpone_moves_reminder_but_preserves_due(self):
        self.send("待辦 今天 09:01 回電")
        self.send("延後 1 今天 09:03")
        self.now += timedelta(minutes=2)
        self.app.tick(self.now)
        self.assertFalse(any(call[0] == "push" for call in self.line.sent))
        self.assertIn("已逾期｜11/20 09:01", self.send("待辦追蹤")["text"])
        self.now += timedelta(minutes=1)
        self.app.tick(self.now)
        self.assertIn("回電", self.line.sent[-1][1]["messages"][0]["text"])

    def test_expenses_idempotence_and_private_access(self):
        self.send("記一筆 午餐 120.50 餐飲", key="redelivery")
        self.send("記一筆 午餐 120.50 餐飲", key="redelivery")
        self.send("記一筆 停車 80 交通 昨天")
        self.assertIn("NT$ 200.50", self.send("月支出")["text"])
        self.assertIn("只有已配對", self.send("月支出", "Ustranger")["text"])
        self.send("作廢支出 2")
        self.assertIn("NT$ 120.50", self.send("月支出")["text"])

    def test_report_excel_formula_safe_and_expiring(self):
        self.send("行程 今天 14:00-15:00 公益拜訪｜分類:1")
        self.send("待辦 =1+1｜分類:4")
        self.send("完成 2 已核對")
        self.send("待辦 下週三 確認合作意願")
        self.now = datetime(2026,11,23,8,0,tzinfo=TZ)
        card = self.send("看週報")
        self.assertEqual(card["type"], "flex")
        action = card["contents"]["footer"]["contents"][0]["action"]
        token = action["uri"].rsplit("/",1)[1]
        raw = self.app.download(token, self.now)
        book = load_workbook(BytesIO(raw))
        self.assertEqual(book.sheetnames, ["工作績效週報表", "日曆行程明細", "整理說明"])
        detail = book["日曆行程明細"]
        self.assertEqual(detail["E3"].value, "=1+1")
        self.assertEqual(detail["E3"].data_type, "s")
        self.assertEqual(detail["J4"].value, "待追蹤")
        self.assertIsNone(self.app.download(token, self.now+timedelta(minutes=11)))
        self.assertIsNone(self.app.download("untrusted", self.now))
        self.now += timedelta(hours=1)
        self.app.tick(self.now)
        self.app.tick(self.now)
        self.assertEqual(sum(call[0] == "push" for call in self.line.sent), 1)

    def test_restart_preserves_data_and_deduplication(self):
        self.send("記一筆 午餐 90 餐飲", key="restart")
        self.app = Service(self.config, self.line)
        self.send("記一筆 午餐 90 餐飲", key="restart")
        self.assertIn("NT$ 90", self.send("月支出")["text"])
        self.assertNotIn("NT$ 180", self.send("月支出")["text"])

    def test_http_health_signature_and_webhook_confirm(self):
        http = ThreadingHTTPServer(("127.0.0.1",0), handler(self.app))
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)
        url = f"http://127.0.0.1:{http.server_port}"
        with urlopen(url+"/healthz") as result:
            self.assertEqual(result.read(), b"ok\n")
        raw = b'{"events":[]}'
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(url+"/webhook", raw, {"X-Line-Signature":"invalid"}))
        self.assertEqual(error.exception.code, 401)
        error.exception.close()
        with urlopen(Request(url+"/webhook", raw, {"X-Line-Signature":self.signature(raw)})) as result:
            self.assertEqual(result.status, 200)

    def test_menu_matches_all_commands(self):
        spec = menu()
        self.assertEqual(tuple(item["action"]["text"] for item in spec["areas"]), LABELS)
        self.assertEqual(sum(item["bounds"]["width"]*item["bounds"]["height"] for item in spec["areas"]), 2500*1686)
        for label in LABELS:
            self.assertIn(self.send(label)["type"], ("text", "flex"))
