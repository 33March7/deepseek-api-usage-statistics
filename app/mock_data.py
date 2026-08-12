"""mock 模式: 生成 24 个月的真实感演示数据, 用于无 token 时预览界面。"""
from __future__ import annotations

import random
from datetime import date, timedelta

from . import db

MODELS = ["deepseek-v4-pro", "deepseek-v4-flash", "deepseek-chat & deepseek-reasoner"]
KEYS = ["默认 API Key", "生产环境", "测试环境"]
WALLETS = ["default"]
CURRENCY = "CNY"

# 各模型每日量级与单价(近似, 仅演示)
MODEL_PROFILE = {
    "deepseek-v4-pro":              {"scale": 1.0, "price_out": 0.002,  "price_in": 0.00016},
    "deepseek-v4-flash":            {"scale": 0.7, "price_out": 0.0008, "price_in": 0.00008},
    "deepseek-chat & deepseek-reasoner": {"scale": 0.4, "price_out": 0.001, "price_in": 0.0001},
}


def _growth_factor(day: date) -> float:
    """随时间增长的系数: 越早越小, 并带周末低谷。"""
    days_since = (day - date(2024, 9, 1)).days
    growth = 1 + max(days_since, 0) * 0.004  # 慢速增长
    weekend = 0.55 if day.weekday() >= 5 else 1.0
    noise = random.uniform(0.75, 1.3)
    return growth * weekend * noise


def _hour_weight(hour: int) -> float:
    """日内权重(演示分时): 9-18 点高峰, 早晚递减, 深夜最低。"""
    if 9 <= hour < 18:
        return 1.0
    if 18 <= hour < 23:
        return 0.55
    if 6 <= hour < 9:
        return 0.35
    return 0.10  # 0-5 点


def make_hourly_rows(amount_rows: list[dict], cost_rows: list[dict],
                     day: date, rnd: random.Random | None = None) -> tuple[list[dict], list[dict]]:
    """按日内权重把某日各「模型 × API Key」组合的日级总量分摊为 24 小时分时行。

    保留模型/Key 维度(弹窗支持按模型/Key 拆分)。
    返回 (hourly_usage 行, hourly_cost 行)。
    """
    day_s = day.isoformat()
    types = ("input_cache_hit_tokens", "input_cache_miss_tokens",
             "output_tokens", "request_count")
    # (model, key) → {type: 日级总量}
    combos: dict[tuple[str, str], dict[str, int]] = {}
    for r in amount_rows:
        if r["utc_date"] != day_s or r["type"] not in types:
            continue
        key = (str(r.get("model") or "unknown"), str(r.get("api_key_name") or ""))
        combos.setdefault(key, {t: 0 for t in types})[r["type"]] += r["amount"]
    # (model, key) → {currency: 日级费用}
    combo_costs: dict[tuple[str, str], dict[str, float]] = {}
    for r in cost_rows:
        if r["utc_date"] != day_s:
            continue
        key = (str(r.get("model") or "unknown"), str(r.get("api_key_name") or ""))
        by_cur = combo_costs.setdefault(key, {})
        by_cur[r["currency"]] = by_cur.get(r["currency"], 0.0) + r["cost"]

    rnd = rnd or random
    usage_rows: list[dict] = []
    cost_out: list[dict] = []
    for (model, key_name), totals in combos.items():
        weights = [_hour_weight(h) * rnd.uniform(0.7, 1.3) for h in range(24)]
        total_w = sum(weights)
        for h in range(24):
            frac = weights[h] / total_w
            for typ, total in totals.items():
                amt = int(total * frac)
                if amt > 0:
                    usage_rows.append({"utc_date": day_s, "hour": h, "model": model,
                                       "api_key_name": key_name, "type": typ, "amount": amt})
        for cur, total in combo_costs.get((model, key_name), {}).items():
            for h in range(24):
                c = round(total * (weights[h] / total_w), 6)
                if c > 0:
                    cost_out.append({"utc_date": day_s, "hour": h, "model": model,
                                     "api_key_name": key_name, "cost": c, "currency": cur})
    return usage_rows, cost_out


def generate(days: int = 730) -> dict:
    """生成/覆盖 mock 数据, 返回写入统计。"""
    # 清空既有数据表(仅 mock 模式使用)
    conn = db.connect()
    conn.execute("DELETE FROM amount_daily")
    conn.execute("DELETE FROM cost_daily")
    conn.execute("DELETE FROM hourly_usage")
    conn.execute("DELETE FROM hourly_cost")
    conn.commit()

    rnd = random.Random(42)
    today = date.today()
    amount_rows: list[dict] = []
    cost_rows: list[dict] = []

    for i in range(days, 0, -1):
        day = today - timedelta(days=i - 1)
        for model, profile in MODEL_PROFILE.items():
            factor = _growth_factor(day) * profile["scale"]
            key_name = rnd.choice(KEYS)
            # 用量行(计费类型, 与真实接口映射后的小写类型一致)
            cache_hit = int(400_000 * factor * rnd.uniform(0.3, 0.8))
            cache_miss = int(250_000 * factor * rnd.uniform(0.4, 1.0))
            output = int(120_000 * factor * rnd.uniform(0.4, 1.0))
            prompt = int(cache_hit + cache_miss)
            requests = int(300 * factor * rnd.uniform(0.5, 1.5))
            for typ, amt in (("input_cache_hit_tokens", cache_hit),
                             ("input_cache_miss_tokens", cache_miss),
                             ("prompt_tokens", prompt),
                             ("output_tokens", output),
                             ("request_count", requests)):
                amount_rows.append({
                    "utc_date": day.isoformat(),
                    "model": model,
                    "api_key_name": key_name,
                    "type": typ,
                    "amount": amt,
                    "price": None,
                })
            # 费用行(输入低价 + 输出高价)
            cost = (cache_hit * profile["price_in"] * 0.1
                    + cache_miss * profile["price_in"]
                    + output * profile["price_out"])
            cost_rows.append({
                "utc_date": day.isoformat(),
                "model": model,
                "api_key_name": key_name,
                "wallet_type": "default",
                "cost": round(cost, 6),
                "currency": CURRENCY,
            })

    n1 = db.upsert_amounts(amount_rows)
    n2 = db.upsert_costs(cost_rows)
    # 分时(小时级): 仅今天+昨天(与平台限制一致), 由日级总量按日内权重分摊
    hourly_usage_rows: list[dict] = []
    hourly_cost_rows: list[dict] = []
    for d in (today, today - timedelta(days=1)):
        u, c = make_hourly_rows(amount_rows, cost_rows, d, rnd)
        hourly_usage_rows += u
        hourly_cost_rows += c
    n3 = db.upsert_hourly_amounts(hourly_usage_rows)
    n4 = db.upsert_hourly_costs(hourly_cost_rows)
    # 标记 mock 数据存在: 下一次真实同步会先自动清空, 避免演示数据混入真实统计
    db.set_meta("mock_generated_at", db.now_iso())
    db.set_meta("last_sync_at", db.now_iso())
    db.set_meta("earliest_date", db.earliest_date())
    db.set_meta("latest_date", db.latest_date())
    db.log_sync_end(
        db.log_sync_start(), "ok",
        f"[mock] 生成 {days} 天演示数据: 用量 {n1} 行, 费用 {n2} 行, 分时 {n3 + n4} 行",
        24, 24,
    )
    return {"amount": n1, "cost": n2, "hourly": n3 + n4}
