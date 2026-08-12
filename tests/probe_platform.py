"""实测 platform.deepseek.com 私有接口的真实返回结构。

用法: py tests/probe_platform.py [月] [年]  (默认当月)
token 从 data/token.json 读取(由登录/同步流程写入), 不在脚本中硬编码。
"""
import json
import sys
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import sync  # noqa: E402
from app.config import RAW_DIR  # noqa: E402

TOKEN = sync.load_token()
if not TOKEN:
    print("请先配置登录(data/token.json 为空)")
    sys.exit(1)

today = date.today()
month = int(sys.argv[1]) if len(sys.argv) > 1 else today.month
year = int(sys.argv[2]) if len(sys.argv) > 2 else today.year

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}
BASE = "https://platform.deepseek.com"


def show(name, resp):
    print(f"\n===== {name} [HTTP {resp.status_code}] =====")
    try:
        data = resp.json()
    except ValueError:
        print("非 JSON:", resp.text[:300])
        return None
    if isinstance(data, dict):
        print("顶层键:", list(data.keys()))
        biz = data.get("data") or {}
        if isinstance(biz, dict) and "biz_data" in biz:
            print("biz_code:", biz.get("biz_code"), "| biz_msg:", str(biz.get("biz_msg"))[:100])
            payload = biz["biz_data"]
            print("biz_data 类型:", type(payload).__name__)
            if isinstance(payload, list):
                print(f"biz_data: list[{len(payload)}] 首元素:", json.dumps(payload[0], ensure_ascii=False)[:600])
            elif isinstance(payload, dict):
                print("biz_data 键:", list(payload.keys()))
                for k, v in payload.items():
                    if isinstance(v, list) and v:
                        print(f"  {k}: list[{len(v)}] 首元素:", json.dumps(v[0], ensure_ascii=False)[:600])
                    elif isinstance(v, dict):
                        print(f"  {k}: dict 键:", list(v.keys())[:20])
                    else:
                        print(f"  {k}: {str(v)[:120]}")
            else:
                print("biz_data:", str(payload)[:300])
        else:
            print("data:", json.dumps(biz, ensure_ascii=False)[:400])
    else:
        print("顶层类型:", type(data).__name__, str(data)[:200])
    return data


RAW_DIR.mkdir(parents=True, exist_ok=True)
for name, path in [
    ("get_user_summary", "/api/v0/users/get_user_summary"),
    (f"usage/amount {year}-{month:02d}", "/api/v0/usage/amount"),
    (f"usage/cost {year}-{month:02d}", "/api/v0/usage/cost"),
]:
    params = {"month": month, "year": year} if "usage/" in name else {}
    resp = requests.get(BASE + path, params=params, headers=HEADERS, timeout=30)
    show(name, resp)
    fn = RAW_DIR / f"probe_{path.split('/')[-1]}.json"
    fn.write_text(json.dumps(resp.json(), ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[saved] {fn}")
