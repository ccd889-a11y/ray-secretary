"""Commands invoked by the six LINE rich-menu areas."""
import re
import secrets
import uuid
from datetime import datetime, time, timedelta
from decimal import Decimal

from domain import TZ, date_text, metadata, normalize, parse_schedule, read_when
from line_api import SAGE, quick, report_card
from report import boundary, snapshot, week_range, workbook

HELP = """Ray 行動祕書
快速新增｜登記行程或待辦
查行程｜今天、明天或本週的行程
待辦追蹤｜查期限、改期、標記完成
看週報｜上週工作摘要與 Excel
記一筆｜登記支出
月支出｜看分類小計與總額"""

EXPENSE_CATEGORIES = ("餐飲", "交通", "日用品", "家庭", "工作", "娛樂", "醫療", "其他")

def money(cents):
    value = f"{Decimal(cents)/100:,.2f}".removesuffix(".00")
    return f"NT$ {value}"

def ts(value):
    return int(value.timestamp())

def enqueue(db, kind, due, record_id=None, key=None):
    db.execute("INSERT OR IGNORE INTO outbox(key,record_id,kind,due,next_attempt) VALUES(?,?,?,?,?)",
               (key or str(uuid.uuid4()), record_id, kind, due, due))

def cancel_reminder(db, record_id):
    db.execute("UPDATE outbox SET state='cancelled' WHERE state='pending' AND record_id=?", (record_id,))

def report_message(db, now, public_url, export=False, monday=None):
    monday, friday = (monday, monday + timedelta(days=4)) if monday else week_range(now)
    data = snapshot(db, monday, friday)
    groups = []
    for rows, label, field, color in zip(data, ("行程回顧", "已完成待辦", "待追蹤"),
                                          ("starts", "completed", "starts"), (SAGE, SAGE, "#F6D98F")):
        lines = []
        for row in rows[:4]:
            day = date_text(row[field], row["all_day"] or field == "completed")
            mark = f'{day} 到期' if label == "待追蹤" and row[field] else day
            lines.append(f'{mark}｜{row["title"]}')
        if len(rows) > 4:
            lines.append(f"另有 {len(rows)-4} 項，詳見 Excel。")
        groups.append((f"{label}　{len(rows)} 項", lines or ["這段期間沒有紀錄"], color))
    excel_url = None
    if export:
        token = secrets.token_urlsafe(32)
        db.execute("INSERT INTO downloads VALUES(?,?,?)", (token, ts(now)+600, workbook(monday, friday, data)))
        excel_url = f"{public_url}/report/{token}"
    return report_card(f"{monday:%m/%d}–{friday:%m/%d}", groups, excel_url)

def application(db, raw, now, public_url):
    text = normalize(raw)
    try:
        return dispatch(db, text, now, public_url)
    except ValueError as error:
        return quick(str(error))

def dispatch(db, text, now, public_url):
    if text == "快速新增":
        return quick("直接輸入要登記的事項。\n行程 明天 14:00-15:00 公益拜訪\n待辦 週五 17:00 確認完賽禮數量\n待辦 整理拜訪筆記\n\n可附加「｜分類:1」及「｜提醒:明天上午 9 點」。")
    if text == "查行程":
        return quick("要查看哪一天的行程？", ("今天", "查行程 今天"), ("明天", "查行程 明天"), ("本週", "查行程 本週"))
    if text == "記一筆":
        return quick("輸入「記一筆 午餐 120 餐飲」。\n補登可加日期，例如「記一筆 午餐 120 餐飲 11/23」。\n分類：" + "、".join(EXPENSE_CATEGORIES))
    if text in ("說明", "選單", "開始"):
        return quick(HELP)
    if text in ("看週報", "上週週報", "查看上週週報"):
        return report_message(db, now, public_url, export=True)
    command, _, rest = text.partition(" ")
    rest = rest.strip()
    if command in ("查行程", "待辦追蹤", "月支出"):
        return lookup(db, command, rest, now)
    if command in ("行程", "待辦"):
        return add_record(db, command, rest, now)
    if command in ("完成", "延後", "改期", "取消"):
        return update_record(db, command, rest, now)
    if command == "記一筆":
        return expense(db, rest, now)
    if command == "作廢支出":
        if not re.fullmatch(r"#?\d{1,12}", rest):
            return quick("請輸入「作廢支出 5」，其中 5 是支出編號。")
        changed = db.execute("UPDATE expenses SET voided=1 WHERE id=? AND voided=0", (int(rest.lstrip("#")),)).rowcount
        return quick("已作廢這筆支出。" if changed else "找不到可作廢的支出。")
    return quick("請點選下方選單，或輸入「說明」查看可用指令。", ("行程或待辦", "快速新增"), ("記支出", "記一筆"))

def add_record(db, command, body, now):
    kind = "event" if command == "行程" else "task"
    start, end, timed = None, None, False
    if kind == "event" or re.match(r"^(今天|明天|後天|(?:(?:本|這|下)?週|本星期|下星期|星期)[一二三四五六日天]|\d{1,4}[-/]\d)", body):
        start, end, timed, body = parse_schedule(body, now)
    if end and kind == "task":
        return quick("待辦只需填一個截止時間。")
    label, category, explicit = metadata(body)
    reminder, precise_reminder = read_when(explicit, now) if explicit else (start, timed)
    if reminder and not precise_reminder:
        reminder = datetime.combine(reminder.date(), time(9), TZ)
    if explicit and reminder <= now:
        return quick("提醒時間已過，請輸入未來時間。")
    record_id = db.execute("INSERT INTO records(kind,title,category,starts,ends,all_day,created) VALUES(?,?,?,?,?,?,?)",
        (kind, label, category, ts(start) if start else None, ts(end) if end else None, int(not timed), ts(now))).lastrowid
    notice = ""
    if reminder and reminder > now:
        enqueue(db, "reminder", ts(reminder), record_id)
        notice = f"\n將於 {reminder:%m/%d %H:%M} 推送提醒。"
    elif reminder:
        notice = "\n提醒時間已過，已保留登記。"
    suffix = f"｜{date_text(ts(start), not timed)}" if start else "｜未設期限"
    return quick(f"已登記{command} #{record_id}｜{label}{suffix}{notice}")

def update_record(db, command, body, now):
    match = re.fullmatch(r"#?(\d{1,12})(?:\s+(.+))?", body)
    if not match:
        return quick("請輸入編號，例如「完成 5 已寄出邀約」或「延後 5 明天 10:00」。")
    record_id, argument = int(match[1]), match[2]
    row = db.execute("SELECT * FROM records WHERE id=? AND status='open'", (record_id,)).fetchone()
    if not row:
        return quick("找不到這筆未結案紀錄。")
    if command == "完成":
        if row["kind"] != "task":
            return quick("行程可改期或取消。完成工作請建立待辦再標記完成。")
        db.execute("UPDATE records SET status='done',completed=?,result=? WHERE id=?", (ts(now), (argument or "")[:240], record_id))
        cancel_reminder(db, record_id)
        return quick(f"已完成 #{record_id}｜{row['title']}")
    if command == "取消":
        db.execute("UPDATE records SET status='cancelled' WHERE id=?", (record_id,))
        cancel_reminder(db, record_id)
        return quick(f"已取消 #{record_id}｜{row['title']}")
    if not argument:
        return quick(f"請輸入「{command} {record_id} 明天 10:00」。")
    value, timed = read_when(argument, now)
    reminder = value if timed else datetime.combine(value.date(), time(9), TZ)
    if reminder <= now:
        return quick("請選擇尚未到達的時間。")
    if command == "改期":
        duration = row["ends"]-row["starts"] if row["ends"] and timed else None
        db.execute("UPDATE records SET starts=?,ends=?,all_day=? WHERE id=?",
                   (ts(value), ts(value)+duration if duration else None, int(not timed), record_id))
    cancel_reminder(db, record_id)
    enqueue(db, "reminder", ts(reminder), record_id)
    action = "改期" if command == "改期" else "延後提醒"
    return quick(f"已{action} #{record_id}，將於 {reminder:%m/%d %H:%M} 提醒。")

def lookup(db, command, rest, now):
    if command == "查行程":
        day = now.date()
        if rest == "本週":
            day -= timedelta(days=day.weekday())
            end = day + timedelta(days=7)
        else:
            day = read_when(rest, now)[0].date() if rest else day
            end = day + timedelta(days=1)
        rows = db.execute("SELECT * FROM records WHERE kind='event' AND status='open' AND starts>=? AND starts<? ORDER BY starts,id", (boundary(day), boundary(end))).fetchall()
        lines = [f"#{r['id']} {date_text(r['starts'], r['all_day'])} {'全天 ' if r['all_day'] else ''}{r['title']}" for r in rows[:20]]
        return quick(f"行程｜{day:%m/%d}–{end-timedelta(days=1):%m/%d}\n" + list_text(lines, len(rows)), ("待辦追蹤", "待辦追蹤"))
    if command == "待辦追蹤":
        if rest and not re.fullmatch(r"[1-9]\d{0,3}", rest):
            return quick("請輸入「待辦追蹤」或「待辦追蹤 2」看第二頁。")
        page = int(rest or 1)
        count = db.execute("SELECT COUNT(*) FROM records WHERE kind='task' AND status='open'").fetchone()[0]
        rows = db.execute("SELECT * FROM records WHERE kind='task' AND status='open' ORDER BY starts IS NULL,starts,id LIMIT 12 OFFSET ?", ((page-1)*12,)).fetchall()
        lines = []
        for r in rows:
            cutoff = r["starts"] + (86400 if r["all_day"] else 0) if r["starts"] else None
            status = "已逾期" if cutoff is not None and cutoff <= ts(now) else "未完成"
            lines.append(f"#{r['id']} {status}｜{date_text(r['starts'],r['all_day'])}｜{r['title']}")
        buttons = [("下一頁", f"待辦追蹤 {page+1}")] if count > page*12 else []
        return quick(f"待辦追蹤｜共 {count} 項\n" + ("\n".join(lines) if lines else "本頁沒有待辦。") +
                     "\n\n例：完成 5／改期 5 明天 10:00／延後 5 明天 10:00", *buttons)
    month = rest or now.strftime("%Y-%m")
    if not re.fullmatch(r"[1-9]\d{3}-(?:0[1-9]|1[0-2])", month):
        return quick("請使用「月支出 2026-11」。")
    groups = db.execute("SELECT category,SUM(cents) AS total,COUNT(*) AS count FROM expenses WHERE voided=0 AND date LIKE ? GROUP BY category ORDER BY total DESC,category", (month+"-%",)).fetchall()
    recent = db.execute("SELECT * FROM expenses WHERE voided=0 AND date LIKE ? ORDER BY date DESC,id DESC LIMIT 5", (month+"-%",)).fetchall()
    result = [f'{r["category"]}　{money(r["total"])}（{r["count"]} 筆）' for r in groups]
    latest = [f'#{r["id"]} {r["date"][5:]} {r["title"]}　{money(r["cents"])}' for r in recent]
    base = f"{month} 月支出｜{money(sum(r['total'] for r in groups))}\n"
    return quick(base + ("\n".join(result) + "\n\n最近五筆\n" + "\n".join(latest) if groups else "這個月尚無紀錄。"))

def list_text(lines, count):
    if not lines:
        return "這段期間沒有行程。"
    return "\n".join(lines) + (f"\n另有 {count-len(lines)} 筆。可指定日期查看。" if count > len(lines) else "")

def expense(db, rest, now):
    suffix = re.search(r"\s+(\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}/\d{1,2}|今天|昨天|前天)$", rest)
    date = now.date()
    if suffix:
        relative = {"昨天": 1, "前天": 2}
        value = suffix[1]
        date = now.date()-timedelta(days=relative[value]) if value in relative else read_when(value, now)[0].date()
        if date > now.date():
            return quick("支出日期不能晚於今天。")
        rest = rest[:suffix.start()]
    match = re.fullmatch(r"(.+?)\s+(\d{1,9}|\d{1,3}(?:,\d{3}){1,2})(\.\d{1,2})?(?:\s+(\S+))?", rest)
    if not match:
        return quick("請輸入「記一筆 午餐 120 餐飲」。")
    label, number, decimals, category = match.groups()
    cents = int(Decimal(number.replace(",", "") + (decimals or ""))*100)
    category = category or "其他"
    if not cents or category not in EXPENSE_CATEGORIES or len(label) > 100:
        return quick("請輸入正金額、100 字內的品項及有效分類：" + "、".join(EXPENSE_CATEGORIES))
    key = db.execute("INSERT INTO expenses(title,category,cents,date) VALUES(?,?,?,?)", (label, category, cents, date.isoformat())).lastrowid
    return quick(f"已登記支出 #{key}｜{date:%m/%d} {label} {money(cents)}｜{category}", ("看月支出", "月支出"))
