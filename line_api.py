"""LINE transport, message buttons and Flex weekly report."""
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = "https://api.line.me/v2/bot"
FOREST = "#164637"
IVORY = "#F8F4EB"
SAGE = "#D6E3C9"

class Line:
    def __init__(self, token):
        self.token = token

    def send(self, endpoint, data, retry_key=None):
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        if retry_key:
            headers["X-Line-Retry-Key"] = retry_key
        request = Request(f"{ROOT}/message/{endpoint}", json.dumps(data, ensure_ascii=False).encode(), headers)
        try:
            with urlopen(request, timeout=6) as result:
                return result.status
        except HTTPError as error:
            return error.code
        except (OSError, URLError):
            return 0

def quick(message, *buttons):
    data = {"type": "text", "text": message[:4800]}
    if buttons:
        data["quickReply"] = {"items": [
            {"type": "action", "action": {"type": "message", "label": label, "text": value}}
            for label, value in buttons
        ]}
    return data

def report_card(period, groups, excel_url=None):
    header = [{"type": "text", "text": "工作週報", "size": "xl", "weight": "bold", "color": "#FFFFFF"},
              {"type": "text", "text": period, "size": "sm", "color": SAGE}]
    sections = []
    for label, lines, color in groups:
        sections.append({"type": "box", "layout": "vertical", "spacing": "sm", "contents": [
            {"type":"box","layout":"vertical","backgroundColor":color,"cornerRadius":"md","paddingAll":"8px","contents":[
                {"type":"text","text":label,"weight":"bold","size":"sm","color":FOREST}
            ]},
            *[{"type": "text", "text": line, "wrap": True, "size": "sm", "color": FOREST} for line in lines],
        ]})
    action = ({"type": "uri", "label": "下載 Excel", "uri": excel_url} if excel_url else
              {"type": "message", "label": "查看週報／下載 Excel", "text": "看週報"})
    return {"type":"flex","altText":f"工作週報 {period}", "contents":{
        "type":"bubble","size":"mega",
        "header":{"type":"box","layout":"vertical","backgroundColor":FOREST,"contents":header},
        "body":{"type":"box","layout":"vertical","spacing":"lg","backgroundColor":IVORY,"contents":sections},
        "footer":{"type":"box","layout":"vertical","backgroundColor":IVORY,"contents":[
            {"type":"button","style":"primary","height":"sm","color":FOREST,"action":action}
        ]},
    }}
