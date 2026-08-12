"""分时接口探针: 实测 by_api_key/amount|cost 是否支持 bucket=3600(小时粒度)。

运行: py -m tests.probe_hourly
作用: 打印各请求的响应 code / biz_data.bucket 回显 / 桶数量与首桶结构,
     原始响应自动存档到 data/raw/(由 sync._get 完成)。
用途: 确认官网「当天/昨天分时」数据来自哪个接口形态; 若 bucket 参数被拒
     (INVALID_PARAM), 说明分时数据另有接口, 需要浏览器抓包校准。
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import sync  # noqa: E402
from app.config import SYNC_TZ  # noqa: E402


def _window(day: date) -> tuple[int, int]:
    """[day 00:00, 次日 00:00) 的 Unix 秒(GMT+8 日边界)。"""
    return sync._day_unix(day), sync._day_unix(day + timedelta(days=1))


def _probe(client, label: str, url: str, params: dict) -> None:
    print(f"\n=== {label} ===")
    print(f"GET {url.split('/')[-1]}?{url.split('?')[-1] if '?' in url else ''} params={params}")
    try:
        data = client._get(url, params)
    except sync.SyncError as e:
        print(f"  [SyncError] {e}")
        return
    payload = sync._biz_payload(data)
    if not isinstance(payload, dict):
        print(f"  响应非预期: {str(data)[:200]}")
        return
    buckets = []
    for s in payload.get("series", []) if "series" in payload else []:
        buckets += s.get("buckets", [])
    data_list = payload.get("data") or []
    cost_buckets = []
    for entry in data_list if isinstance(data_list, list) else []:
        for s in entry.get("series", []):
            cost_buckets += s.get("buckets", [])
    print(f"  code={data.get('code')} biz_code={payload.get('biz_code')} "
          f"bucket={payload.get('bucket')} start={payload.get('start')} end={payload.get('end')}")
    print(f"  amount 桶数: {len(buckets)}   cost 桶数: {len(cost_buckets)}")
    if buckets:
        b = buckets[0]
        print(f"  首桶 amount: time={b.get('time')}(= {sync._ts_to_date(b.get('time'))}) "
              f"usage={list((b.get('usage') or {}).keys())}")
        print(f"  末桶 amount: time={buckets[-1].get('time')}")
    if cost_buckets:
        b = cost_buckets[0]
        print(f"  首桶 cost:   time={b.get('time')} cost={b.get('cost')}")


def main() -> int:
    token = sync.load_token()
    if not token:
        print("未找到已保存的登录凭证(data/token.json), 请先登录后再探测")
        return 1
    client = sync.PlatformClient(token)

    today = datetime.now(SYNC_TZ).date()
    yesterday = today - timedelta(days=1)
    s_t, e_t = _window(today)
    s_y, e_y = _window(yesterday)

    print(f"今天(GMT+8)={today}  昨天={yesterday}")
    print(f"今天窗口: start={s_t} end={e_t}  昨天窗口: start={s_y} end={e_y}")

    # 对照: 不带 bucket 的日级请求(基线)
    _probe(client, "对照: 日级(不带 bucket)", sync.USAGE_BY_KEY_AMOUNT_URL,
           {"start": s_y, "end": e_t, "tz": sync.SYNC_TZ_OFFSET_SEC})

    # 核心: bucket=3600 小时粒度
    _probe(client, "今天分时 amount bucket=3600", sync.USAGE_BY_KEY_AMOUNT_URL,
           {"start": s_t, "end": e_t, "tz": sync.SYNC_TZ_OFFSET_SEC, "bucket": 3600})
    _probe(client, "昨天分时 amount bucket=3600", sync.USAGE_BY_KEY_AMOUNT_URL,
           {"start": s_y, "end": e_y, "tz": sync.SYNC_TZ_OFFSET_SEC, "bucket": 3600})
    _probe(client, "今天分时 cost bucket=3600", sync.USAGE_BY_KEY_COST_URL,
           {"start": s_t, "end": e_t, "tz": sync.SYNC_TZ_OFFSET_SEC, "bucket": 3600})
    _probe(client, "昨天分时 cost bucket=3600", sync.USAGE_BY_KEY_COST_URL,
           {"start": s_y, "end": e_y, "tz": sync.SYNC_TZ_OFFSET_SEC, "bucket": 3600})

    print("\n探测完成。若 bucket=3600 被拒(INVALID_PARAM 等), 请在浏览器打开平台用量页,")
    print("F12 → Network → 切换「分时」视图, 找到实际请求的 URL/参数后告知校准。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
