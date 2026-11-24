"""Taiwan date parsing and presentation."""
import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Taipei")
WEEKDAYS = "一二三四五六日"
CATEGORIES = (
    "主要目標－募集社會資源",
    "次要目標－公關、義工、倉管",
    "其他目標－到宅／參訪／說明／發票",
    "行政庶務、業務認知",
)
DATE = r"(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}/\d{1,2}|今天|明天|後天|(?:(?:本|這|下)?週|本星期|下星期|星期)[一二三四五六日天])"
CLOCK = r"(?:早上|上午|中午|下午|傍晚|晚上)?\s*\d{1,2}(?::\d{2}|點(?:半|\d{1,2}分)?)"
WHEN = re.compile(rf"(?P<day>{DATE})(?:\s*(?P<clock>{CLOCK}))?")

def normalize(text):
    table = str.maketrans("０１２３４５６７８９：／｜－，　", "0123456789:/|-, ")
    return text.translate(table).replace("周", "週").strip()

def day_value(value, now):
    relative = {"今天": 0, "明天": 1, "後天": 2}
    if value in relative:
        return now.date() + timedelta(days=relative[value])
    if value[-1] in WEEKDAYS + "天":
        weekday = WEEKDAYS.index(value[-1].replace("天", "日"))
        offset = weekday - now.weekday()
        if value.startswith("下"):
            offset += 7
        elif not value.startswith(("本", "這")) and offset < 0:
            offset += 7
        return now.date() + timedelta(days=offset)
    parts = value.replace("-", "/").split("/")
    try:
        return datetime(*map(int, parts)).date() if len(parts) == 3 else datetime(now.year, *map(int, parts)).date()
    except ValueError:
        raise ValueError("日期不正確，請用 2026/12/01 或明天。") from None

def clock_value(value):
    value = value.replace(" ", "")
    match = re.fullmatch(r"(早上|上午|中午|下午|傍晚|晚上)?(\d{1,2})(?::(\d{2})|點(?:(半)|(\d{1,2})分)?)", value)
    if not match:
        raise ValueError("時間格式請用 14:30 或下午 2 點半。")
    period, hour_text, colon, half, minutes = match.groups()
    hour = int(hour_text)
    if period and not 1 <= hour <= 12:
        raise ValueError("請選擇 24 小時制，或用上午／下午加 1～12 點。")
    if period in ("中午", "下午", "傍晚", "晚上") and hour < 12:
        hour += 12
    if period in ("上午", "早上") and hour == 12:
        hour = 0
    try:
        return time(hour, 30 if half else int(colon or minutes or 0))
    except ValueError:
        raise ValueError("時間超出範圍。") from None

def read_when(value, now):
    match = WHEN.fullmatch(value.strip())
    if not match:
        raise ValueError("日期時間請用「明天下午 2 點」或「2026/12/01 14:00」。")
    clock = match["clock"]
    return datetime.combine(day_value(match["day"], now), clock_value(clock) if clock else time(), TZ), bool(clock)

def parse_schedule(body, now):
    match = WHEN.match(body)
    if not match:
        raise ValueError("請以日期時間開頭，例如「行程 明天下午 2 點 公益拜訪」。")
    start, timed = read_when(match[0], now)
    remainder = body[match.end():].strip()
    end = None
    if remainder.startswith(("-", "～", "~", "至", "–")):
        finish = re.match(rf"[-～~至–]\s*({CLOCK})(?=\s|$)", remainder)
        if not finish or not timed:
            raise ValueError("結束時間請用「明天 14:00-15:00 公益拜訪」。")
        end = datetime.combine(start.date(), clock_value(finish[1]), TZ)
        if end <= start:
            raise ValueError("結束時間必須晚於開始時間。")
        remainder = remainder[finish.end():].strip()
    return start, end, timed, remainder

def metadata(value):
    parts = [part.strip() for part in value.split("|")]
    title = parts.pop(0)
    if not title or len(title) > 160:
        raise ValueError("請輸入 1～160 字的事項名稱。")
    fields = {}
    for part in parts:
        key, sep, data = part.partition(":")
        if not sep or not data.strip() or key not in ("分類", "提醒") or key in fields:
            raise ValueError("附加資料請用「｜分類:1｜提醒:明天 09:00」。")
        fields[key] = data.strip()
    category = fields.get("分類", "")
    if category and category not in "1234":
        raise ValueError("工作分類請輸入 1、2、3 或 4。")
    if category and len(category) != 1:
        raise ValueError("工作分類請輸入 1、2、3 或 4。")
    return title, int(category) if category else None, fields.get("提醒")

def date_text(value, all_day=False):
    if value is None:
        return "未設期限"
    value = datetime.fromtimestamp(value, TZ)
    return value.strftime("%m/%d") if all_day else value.strftime("%m/%d %H:%M")
