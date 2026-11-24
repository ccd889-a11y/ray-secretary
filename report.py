"""Weekly snapshot and the work-performance Excel export."""
from datetime import datetime, time, timedelta
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from domain import CATEGORIES, TZ, WEEKDAYS, date_text

def boundary(day):
    return int(datetime.combine(day, time(), TZ).timestamp())

def week_range(now, offset=-1):
    monday = now.date() - timedelta(days=now.weekday()) + timedelta(weeks=offset)
    return monday, monday + timedelta(days=4)

def snapshot(db, monday, friday):
    start, end = boundary(monday), boundary(friday + timedelta(days=1))
    events = db.execute(
        "SELECT * FROM records WHERE kind='event' AND status <> 'cancelled' AND starts>=? AND starts<? ORDER BY starts,id",
        (start, end),
    ).fetchall()
    completed = db.execute(
        "SELECT * FROM records WHERE kind='task' AND status='done' AND completed>=? AND completed<? ORDER BY completed,id",
        (start, end),
    ).fetchall()
    pending = db.execute(
        """SELECT * FROM records WHERE kind='task' AND status='open'
           AND (created<? OR starts<?)
           ORDER BY starts IS NULL,starts,id""",
        (end, end),
    ).fetchall()
    return events, completed, pending

def record_date(row, field):
    return datetime.fromtimestamp(row[field], TZ).date()

def work_hours(rows):
    durations = [row["ends"] - row["starts"] for row in rows if row["ends"] and not row["all_day"]]
    return round(sum(durations) / 3600, 2) if durations else None

def workbook(monday, friday, data):
    events, completed, pending = data
    book = Workbook()
    summary = book.active
    summary.title = "工作績效週報表"
    summary.append(["工作績效週報表"])
    summary.append([f"{monday:%Y/%m/%d}～{friday:%Y/%m/%d}　斗六分院　陳建地"])
    summary.merge_cells("A1:K1")
    summary.merge_cells("A2:K2")
    summary.append(["月／日", "週", "預計工作摘要", "數據(H)", "實際執行情況", "數據", "達成與否", "成長％", "效益", "檢討改進與建議", "主管（含人事部門）意見"])
    for offset in range(5):
        day = monday + timedelta(days=offset)
        day_events = [r for r in events if record_date(r, "starts") == day]
        day_done = [r for r in completed if record_date(r, "completed") == day]
        codes = [1, 2, 3, 4]
        if any(r["category"] is None for r in day_events + day_done):
            codes.append(None)
        for code in codes:
            planned = [r for r in day_events if r["category"] == code]
            done = [r for r in day_done if r["category"] == code]
            label = f"{code}. {CATEGORIES[code-1]}" if code else "待分類"
            plan_lines = [f'{date_text(r["starts"], r["all_day"])}　{r["title"]}' for r in planned]
            done_lines = [r["title"] + (f'｜{r["result"]}' if r["result"] else "") for r in done]
            summary.append([day, WEEKDAYS[day.weekday()], "\n".join([label, *plan_lines]), work_hours(planned), "\n".join(done_lines)])
            summary.cell(summary.max_row, 1).number_format = "mm/dd"
    summary.append(["核示：　人力發展室：　　　　　　　　　部門／單位主管：　　　　　　　　　工作人員：陳建地"])
    summary.merge_cells(start_row=summary.max_row, start_column=1, end_row=summary.max_row, end_column=11)

    detail = book.create_sheet("日曆行程明細")
    detail.append(["日期／效期", "星期", "開始", "結束", "事項名稱", "工作類別", "行程時數(H)", "來源", "類型", "狀態", "完成結果"])
    for rows, kind, status, date_field in (
        (events, "行程", "已登記", "starts"),
        (completed, "待辦", "已完成", "completed"),
        (pending, "待辦", "待追蹤", "starts"),
    ):
        for row in rows:
            day = record_date(row, date_field) if row[date_field] is not None else None
            detail.append([
                day, WEEKDAYS[day.weekday()] if day else None,
                ("全天" if row["all_day"] else date_text(row["starts"])[6:]) if kind == "行程" else None,
                date_text(row["ends"])[6:] if row["ends"] and kind == "行程" else None,
                row["title"], CATEGORIES[row["category"]-1] if row["category"] else "待分類",
                work_hours([row]) if kind == "行程" else None, "LINE 行動祕書", kind, status, row["result"],
            ])
            detail.cell(detail.max_row, 1).number_format = "yyyy/mm/dd"

    notes = book.create_sheet("整理說明")
    for row in (
        ["項目", "說明"],
        ["範圍", "臺灣時間上週一至週五。待追蹤列出週五前已建立或已到期且目前未完成的待辦。"],
        ["資料來源", "Ray 行動祕書的本地紀錄。"],
        ["完成狀態", "已登記行程列入預計工作；只有手動標記完成的待辦列入實際執行情況。"],
        ["工時", "依起訖計算，未填結束時間或全天留白。重疊時段未扣除，不能視為出勤工時。"],
        ["分類", "輸入時可加上「｜分類:1～4」。未指定者保留待分類。"],
        ["評核", "主管意見、執行數據與效益保留空白供人工填寫。"],
        ["行程／已完成／待追蹤", f"{len(events)}／{len(completed)}／{len(pending)}"],
    ):
        notes.append(row)

    for sheet, header, widths in (
        (summary, 3, [13, 8, 54, 11, 52, 11, 12, 10, 19, 25, 30]),
        (detail, 1, [17, 8, 12, 12, 51, 36, 16, 18, 10, 13, 42]),
        (notes, 1, [25, 102]),
    ):
        sheet.freeze_panes = f"A{header+1}"
        for index, width in enumerate(widths, 1):
            sheet.column_dimensions[get_column_letter(index)].width = width
        for cells in sheet.iter_rows():
            for cell in cells:
                cell.alignment = Alignment(vertical="top", wrap_text=True)
                if isinstance(cell.value, str):
                    cell.data_type = "s"
                if cell.row == header:
                    cell.fill = PatternFill("solid", fgColor="164637")
                    cell.font = Font(name="Microsoft JhengHei", bold=True, color="FFFFFF")
                else:
                    cell.font = Font(name="Microsoft JhengHei", color="164637")
                    if cell.row > header and cell.row % 2 == 0:
                        cell.fill = PatternFill("solid", fgColor="F3F5EE")
            if cells[0].row > header:
                lines = max((sum(max(1, (len(part) + 23) // 24) for part in cell.value.splitlines())
                             for cell in cells if isinstance(cell.value, str)), default=1)
                sheet.row_dimensions[cells[0].row].height = min(409, max(24, 17 * lines))
    summary["A1"].font = Font(name="Microsoft JhengHei", size=19, bold=True, color="164637")
    summary.row_dimensions[1].height = 31
    detail.auto_filter.ref = detail.dimensions
    data_file = BytesIO()
    book.save(data_file)
    return data_file.getvalue()
