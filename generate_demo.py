"""按真实使用画像生成演示数据(写入独立演示库 data/mock.db)。

画像来源: 实时库中的实际用量 —— 使用相同模型、相同的缓存/输出比例、
相同的请求规模与单价, 再叠加时间增长与日常波动。

用法: py generate_demo.py [天数]   (默认 700 天, 每天均含分时数据)
"""
import random
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.config import DB_PATH, DB_PATH_MOCK, SYNC_TZ  # noqa: E402
from app.mock_data import make_hourly_rows  # noqa: E402

DEMO_KEYS = ["Demo-Key-1", "Demo-Key-2"]


def live_profile() -> dict:
    """从实时库提取各模型的使用画像。"""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    amount = conn.execute("""
        SELECT model,
               SUM(CASE WHEN type='input_cache_hit_tokens' THEN amount ELSE 0 END) AS hit,
               SUM(CASE WHEN type='input_cache_miss_tokens' THEN amount ELSE 0 END) AS miss,
               SUM(CASE WHEN type='output_tokens' THEN amount ELSE 0 END)           AS out,
               SUM(CASE WHEN type='request_count' THEN amount ELSE 0 END)           AS req
        FROM amount_daily WHERE type != 'request_count' GROUP BY model
    """).fetchall()
    cost = {r["model"]: r["c"] for r in conn.execute(
        "SELECT model, SUM(cost) AS c FROM cost_daily GROUP BY model")}
    span = conn.execute("SELECT MIN(utc_date) AS s, MAX(utc_date) AS e FROM amount_daily").fetchone()
    conn.close()

    models = []
    total = 0
    for r in amount:
        tok = r["hit"] + r["miss"] + r["out"]
        total += tok
        models.append({
            "name": r["model"],
            "tokens": tok,
            "hit_ratio": r["hit"] / tok if tok else 0.8,
            "miss_ratio": r["miss"] / tok if tok else 0.1,
            "out_ratio": r["out"] / tok if tok else 0.1,
            "tokens_per_req": tok / r["req"] if r["req"] else 2000,
            "price_per_token": (cost.get(r["model"]) or 0) / tok if tok else 0.001,
        })
    if not models:
        models = [{"name": "deepseek-v4-pro", "tokens": 1, "hit_ratio": .8,
                   "miss_ratio": .1, "out_ratio": .1, "tokens_per_req": 2000,
                   "price_per_token": 0.001}]
        total = 1

    n_days = 1
    if span and span["s"] and span["e"]:
        n_days = max(1, (date_parse(span["e"]) - date_parse(span["s"])).days + 1)
    return {"models": models, "daily_avg": total / n_days, "n_days": n_days}


def date_parse(s: str):
    y, m, d = s.split("-")
    return datetime(int(y), int(m), int(d)).date()


def generate(days: int = 700) -> dict:
    rnd = random.Random(20260812)
    profile = live_profile()
    today = datetime.now(SYNC_TZ).date()
    span = today - timedelta(days=days - 1)

    amount_rows: list[dict] = []
    cost_rows: list[dict] = []
    hourly_usage_rows: list[dict] = []
    hourly_cost_rows: list[dict] = []
    totals = {"tokens": 0, "requests": 0, "cost": 0.0}

    for i in range(days):
        day = today - timedelta(days=i)
        # 时间增长: 越早用量越低; 周末低谷; 随机波动
        growth = 0.45 + 0.55 * (1 - i / days)
        weekend = 0.6 if day.weekday() >= 5 else 1.0
        day_amounts: list[dict] = []
        day_costs: list[dict] = []
        for mi, m in enumerate(profile["models"]):
            share = m["tokens"] / sum(x["tokens"] for x in profile["models"])
            noise = rnd.uniform(0.7, 1.4)
            tokens = int(profile["daily_avg"] * share * growth * weekend * noise)
            if tokens <= 0:
                continue
            hit = int(tokens * m["hit_ratio"])
            miss = int(tokens * m["miss_ratio"])
            out = tokens - hit - miss
            req = max(1, int(tokens / m["tokens_per_req"]))
            key = DEMO_KEYS[mi % len(DEMO_KEYS)]
            for typ, amt in (("input_cache_hit_tokens", hit),
                             ("input_cache_miss_tokens", miss),
                             ("output_tokens", out),
                             ("request_count", req)):
                row = {"utc_date": day.isoformat(), "model": m["name"],
                       "api_key_name": key, "type": typ,
                       "amount": amt, "price": None}
                amount_rows.append(row)
                day_amounts.append(row)
            cost = tokens * m["price_per_token"] * rnd.uniform(0.9, 1.1)
            c_row = {"utc_date": day.isoformat(), "model": m["name"],
                     "api_key_name": key, "wallet_type": "default",
                     "cost": round(cost, 6), "currency": "CNY"}
            cost_rows.append(c_row)
            day_costs.append(c_row)
            totals["tokens"] += tokens
            totals["requests"] += req
            totals["cost"] += cost

        # 每天均生成分时(小时级)数据: 按日内权重分摊当天日级总量
        u, c = make_hourly_rows(day_amounts, day_costs, day, rnd)
        hourly_usage_rows += u
        hourly_cost_rows += c

    # 写入演示库(先清空)
    db.set_mode("mock")
    conn = db.connect()
    conn.execute("DELETE FROM amount_daily")
    conn.execute("DELETE FROM cost_daily")
    conn.execute("DELETE FROM hourly_usage")
    conn.execute("DELETE FROM hourly_cost")
    conn.execute("DELETE FROM sync_log")
    conn.commit()
    n1 = db.upsert_amounts(amount_rows)
    n2 = db.upsert_costs(cost_rows)
    n3 = db.upsert_hourly_amounts(hourly_usage_rows)
    n4 = db.upsert_hourly_costs(hourly_cost_rows)
    db.set_meta("last_sync_at", db.now_iso())
    db.set_meta("earliest_date", db.earliest_date())
    db.set_meta("latest_date", db.latest_date())
    months = max(1, round(days / 30))
    db.log_sync_end(db.log_sync_start(), "ok",
                    f"[演示] 按真实画像生成 {days} 天数据(含分时 {n3 + n4} 行)", months, months)
    return {"days": days, "rows_amount": n1, "rows_cost": n2, "rows_hourly": n3 + n4,
            "span": f"{span} ~ {today}",
            "totals": {k: (round(v, 2) if k == "cost" else v) for k, v in totals.items()},
            "models": [m["name"] for m in profile["models"]]}


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 700
    result = generate(days)
    print("演示数据已生成 →", DB_PATH_MOCK)
    for k, v in result.items():
        print(f"  {k}: {v}")
