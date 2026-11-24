"""Install the six-button menu on the new dedicated LINE account."""
import argparse
import getpass
import json
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

API = "https://api.line.me/v2/bot"
DATA = "https://api-data.line.me/v2/bot"
LABELS = ("快速新增", "查行程", "待辦追蹤", "看週報", "記一筆", "月支出")

def menu():
    xs = [0, 833, 1667, 2500]
    areas = []
    for index, label in enumerate(LABELS):
        row, col = divmod(index, 3)
        areas.append({"bounds": {"x": xs[col], "y": 843*row, "width": xs[col+1]-xs[col], "height": 843},
                      "action": {"type": "message", "label": label, "text": label}})
    return {"size": {"width": 2500, "height": 1686}, "selected": True,
            "name": "Ray 行動祕書 六格選單", "chatBarText": "Ray 行動祕書", "areas": areas}

def request(url, token, payload=None, kind="application/json"):
    if isinstance(payload, dict):
        payload = json.dumps(payload, ensure_ascii=False).encode()
    req = Request(url, payload, {"Authorization": f"Bearer {token}", "Content-Type": kind})
    with urlopen(req, timeout=20) as result:
        content = result.read()
        return json.loads(content) if content else {}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inspect", action="store_true", help="Print the six tappable areas without calling LINE.")
    args = parser.parse_args()
    if args.inspect:
        print(json.dumps(menu(), ensure_ascii=False, indent=2))
        return
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN") or getpass.getpass("Channel access token（輸入時不顯示）: ")
    if not token.strip():
        parser.exit(1, "需要 LINE Channel access token。\n")
    try:
        bot = request(f"{API}/info", token)
        print(f'將安裝到 {bot["displayName"]}（{bot["basicId"]}）')
        if input("確認這是新的專用官方帳號，輸入 INSTALL 繼續: ") != "INSTALL":
            return
        request(f"{API}/richmenu/validate", token, menu())
        menu_id = request(f"{API}/richmenu", token, menu())["richMenuId"]
        print(f"已建立選單 {menu_id}")
        image = Path(__file__).with_name("assets") / "Ray-LINE-Rich-Menu.png"
        request(f"{DATA}/richmenu/{menu_id}/content", token, image.read_bytes(), "image/png")
        request(f"{API}/user/all/richmenu/{menu_id}", token, b"")
        print("已上傳圖片並啟用預設選單。")
    except HTTPError as error:
        parser.exit(1, f"LINE API 回應 HTTP {error.code}。請確認權杖及該帳號的 Messaging API 設定。\n")

if __name__ == "__main__":
    main()
