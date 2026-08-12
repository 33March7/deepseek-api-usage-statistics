"""SQLite 存储层:schema 定义 + 写入/查询助手。

线程安全说明:Python sqlite3 默认每个连接独立,同步线程与 API 线程
各用各的连接(见 connect()),写入时开启 WAL 供并发读。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from .config import DB_PATH, DB_PATH_MOCK, DB_PATH_TEST, ensure_dirs

_local = threading.local()

# 模式 → 数据库文件: 实时与演示(mock)数据完全隔离, 互不污染
_db_mode = "live"


def set_mode(mode: str) -> None:
    """切换数据库模式(live=主库 / mock=演示库 / test=测试库), 并重置连接缓存。"""
    global _db_mode
    if mode not in ("live", "mock", "test"):
        mode = "live"
    if _db_mode != mode:
        _db_mode = mode
        reset_connections()


def _db_path():
    if _db_mode == "mock":
        return DB_PATH_MOCK
    if _db_mode == "test":
        return DB_PATH_TEST
    return DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS amount_daily (
    utc_date     TEXT    NOT NULL,   -- YYYY-MM-DD (UTC)
    model        TEXT    NOT NULL,
    api_key_name TEXT    NOT NULL DEFAULT '',
    type         TEXT    NOT NULL,   -- request_count / input_cache_hit_tokens / ...
    amount       INTEGER NOT NULL,
    price        REAL,               -- 平台返回的单位价格(可能为空)
    PRIMARY KEY (utc_date, model, api_key_name, type)
);

CREATE TABLE IF NOT EXISTS cost_daily (
    utc_date    TEXT NOT NULL,       -- YYYY-MM-DD (按统计时区 GMT+8 的本地日)
    model       TEXT NOT NULL,
    api_key_name TEXT NOT NULL DEFAULT '',
    wallet_type TEXT NOT NULL,       -- 兼容字段(当前接口无钱包拆分)
    cost        REAL NOT NULL,
    currency    TEXT NOT NULL,
    PRIMARY KEY (utc_date, model, api_key_name, currency)
);

CREATE TABLE IF NOT EXISTS sync_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    months_amount INTEGER DEFAULT 0,
    months_cost   INTEGER DEFAULT 0,
    status        TEXT NOT NULL,     -- running / ok / error
    message       TEXT
);

-- 分时(小时级)用量, 仅当天/昨天有数据(平台限制)。按 GMT+8 本地日 + 小时存储,
-- 保留模型/API Key 维度(弹窗支持按模型/Key 拆分)。
CREATE TABLE IF NOT EXISTS hourly_usage (
    utc_date     TEXT    NOT NULL,   -- YYYY-MM-DD (GMT+8 本地日)
    hour         INTEGER NOT NULL,   -- 0-23
    model        TEXT    NOT NULL DEFAULT '',
    api_key_name TEXT    NOT NULL DEFAULT '',
    type         TEXT    NOT NULL,   -- request_count / input_cache_hit_tokens / ...
    amount       INTEGER NOT NULL,
    PRIMARY KEY (utc_date, hour, model, api_key_name, type)
);

CREATE TABLE IF NOT EXISTS hourly_cost (
    utc_date     TEXT NOT NULL,
    hour         INTEGER NOT NULL,
    model        TEXT NOT NULL DEFAULT '',
    api_key_name TEXT NOT NULL DEFAULT '',
    cost         REAL NOT NULL,
    currency     TEXT NOT NULL,
    PRIMARY KEY (utc_date, hour, model, api_key_name, currency)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_amount_date ON amount_daily(utc_date);
CREATE INDEX IF NOT EXISTS idx_amount_model ON amount_daily(model);
CREATE INDEX IF NOT EXISTS idx_cost_date ON cost_daily(utc_date);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect() -> sqlite3.Connection:
    """获取当前线程的数据库连接(懒创建, 指向当前模式对应的库)。"""
    conn = getattr(_local, "conn", None)
    if conn is None:
        ensure_dirs()
        conn = sqlite3.connect(_db_path(), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(SCHEMA)
        _migrate(conn)
        _local.conn = conn
    return conn


def reset_connections() -> None:
    """关闭当前线程连接并清空缓存(模式切换后调用)。"""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        _local.conn = None


def _migrate(conn: sqlite3.Connection) -> None:
    """旧版 cost_daily 无 api_key_name 列: 重建表(数据可随时重新同步)。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(cost_daily)").fetchall()}
    if "api_key_name" not in cols:
        conn.execute("DROP TABLE cost_daily")
        conn.execute("""CREATE TABLE cost_daily (
            utc_date TEXT NOT NULL, model TEXT NOT NULL,
            api_key_name TEXT NOT NULL DEFAULT '',
            wallet_type TEXT NOT NULL DEFAULT 'default',
            cost REAL NOT NULL, currency TEXT NOT NULL,
            PRIMARY KEY (utc_date, model, api_key_name, currency))""")
        conn.commit()

    # v0.1.1 分时表升级: 旧表无 model 列(按总量聚合) → 重建为带维度表。
    # 分时数据每次同步都会重抓当天+昨天, 重建无数据损失。
    hcols = {r[1] for r in conn.execute("PRAGMA table_info(hourly_usage)").fetchall()}
    if hcols and "model" not in hcols:
        conn.execute("DROP TABLE hourly_usage")
        conn.execute("DROP TABLE hourly_cost")
        conn.execute("""CREATE TABLE hourly_usage (
            utc_date TEXT NOT NULL, hour INTEGER NOT NULL,
            model TEXT NOT NULL DEFAULT '', api_key_name TEXT NOT NULL DEFAULT '',
            type TEXT NOT NULL, amount INTEGER NOT NULL,
            PRIMARY KEY (utc_date, hour, model, api_key_name, type))""")
        conn.execute("""CREATE TABLE hourly_cost (
            utc_date TEXT NOT NULL, hour INTEGER NOT NULL,
            model TEXT NOT NULL DEFAULT '', api_key_name TEXT NOT NULL DEFAULT '',
            cost REAL NOT NULL, currency TEXT NOT NULL,
            PRIMARY KEY (utc_date, hour, model, api_key_name, currency))""")
        conn.commit()


# ---------- 写入 ----------

def upsert_amounts(rows: list[dict]) -> int:
    """按主键(utc_date, model, api_key_name, type)覆盖写入用量明细,返回写入行数。"""
    if not rows:
        return 0
    conn = connect()
    conn.executemany(
        """INSERT OR REPLACE INTO amount_daily
           (utc_date, model, api_key_name, type, amount, price)
           VALUES (:utc_date, :model, :api_key_name, :type, :amount, :price)""",
        rows,
    )
    conn.commit()
    return len(rows)


def upsert_costs(rows: list[dict]) -> int:
    """按主键(utc_date, model, api_key_name, currency)覆盖写入费用明细,返回写入行数。"""
    if not rows:
        return 0
    conn = connect()
    conn.executemany(
        """INSERT OR REPLACE INTO cost_daily
           (utc_date, model, api_key_name, wallet_type, cost, currency)
           VALUES (:utc_date, :model, :api_key_name, :wallet_type, :cost, :currency)""",
        rows,
    )
    conn.commit()
    return len(rows)


def upsert_hourly_amounts(rows: list[dict]) -> int:
    """按主键(utc_date, hour, model, api_key_name, type)覆盖写入分时用量,返回写入行数。"""
    if not rows:
        return 0
    conn = connect()
    conn.executemany(
        """INSERT OR REPLACE INTO hourly_usage
           (utc_date, hour, model, api_key_name, type, amount)
           VALUES (:utc_date, :hour, :model, :api_key_name, :type, :amount)""",
        rows,
    )
    conn.commit()
    return len(rows)


def upsert_hourly_costs(rows: list[dict]) -> int:
    """按主键(utc_date, hour, model, api_key_name, currency)覆盖写入分时费用,返回写入行数。"""
    if not rows:
        return 0
    conn = connect()
    conn.executemany(
        """INSERT OR REPLACE INTO hourly_cost
           (utc_date, hour, model, api_key_name, cost, currency)
           VALUES (:utc_date, :hour, :model, :api_key_name, :cost, :currency)""",
        rows,
    )
    conn.commit()
    return len(rows)


def log_sync_start() -> int:
    conn = connect()
    cur = conn.execute(
        "INSERT INTO sync_log (started_at, status) VALUES (?, 'running')",
        (now_iso(),),
    )
    conn.commit()
    return cur.lastrowid


def log_sync_end(log_id: int, status: str, message: str = "",
                 months_amount: int = 0, months_cost: int = 0) -> None:
    conn = connect()
    conn.execute(
        """UPDATE sync_log SET finished_at=?, status=?, message=?,
           months_amount=?, months_cost=? WHERE id=?""",
        (now_iso(), status, message, months_amount, months_cost, log_id),
    )
    conn.commit()


def set_meta(key: str, value: Any) -> None:
    conn = connect()
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
        (key, json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value),
    )
    conn.commit()


def get_meta(key: str, default: Any = None) -> Any:
    conn = connect()
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    return row["value"]


# ---------- 查询 ----------

def earliest_date() -> Optional[str]:
    conn = connect()
    row = conn.execute("SELECT MIN(utc_date) AS d FROM amount_daily").fetchone()
    return row["d"]


def latest_date() -> Optional[str]:
    conn = connect()
    row = conn.execute("SELECT MAX(utc_date) AS d FROM amount_daily").fetchone()
    return row["d"]


def get_sync_logs(limit: int = 20) -> list[dict]:
    conn = connect()
    rows = conn.execute(
        "SELECT * FROM sync_log ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_amount_totals(since: Optional[str] = None) -> dict:
    """按计费类型汇总 tokens 与请求数。返回 {type: amount}。"""
    conn = connect()
    sql = "SELECT type, SUM(amount) AS total FROM amount_daily"
    params: tuple = ()
    if since:
        sql += " WHERE utc_date >= ?"
        params = (since,)
    sql += " GROUP BY type"
    rows = conn.execute(sql, params).fetchall()
    return {r["type"]: r["total"] for r in rows}


def get_cost_totals(since: Optional[str] = None) -> dict:
    """按 (currency, wallet_type) 汇总费用。返回 {(currency, wallet): total}。"""
    conn = connect()
    sql = "SELECT currency, wallet_type, SUM(cost) AS total FROM cost_daily"
    params: tuple = ()
    if since:
        sql += " WHERE utc_date >= ?"
        params = (since,)
    sql += " GROUP BY currency, wallet_type"
    rows = conn.execute(sql, params).fetchall()
    return {(r["currency"], r["wallet_type"]): r["total"] for r in rows}


def get_daily_by_model(start: str, end: str) -> list[dict]:
    """日期 × 模型 的 token 用量(所有计费类型合计),按日期升序。"""
    conn = connect()
    rows = conn.execute(
        """SELECT utc_date, model,
                  SUM(CASE WHEN type='input_cache_hit_tokens' THEN amount ELSE 0 END)   AS cache_hit,
                  SUM(CASE WHEN type='input_cache_miss_tokens' THEN amount ELSE 0 END)   AS cache_miss,
                  SUM(CASE WHEN type='output_tokens' THEN amount ELSE 0 END)             AS output,
                  SUM(CASE WHEN type='request_count' THEN amount ELSE 0 END)             AS requests
           FROM amount_daily
           WHERE utc_date BETWEEN ? AND ? AND type != 'request_count'
           GROUP BY utc_date, model
           ORDER BY utc_date""",
        (start, end),
    ).fetchall()
    return [dict(r) for r in rows]


def get_daily_by_key(start: str, end: str) -> list[dict]:
    """日期 × API Key 的 token 用量(所有计费类型合计),按日期升序。"""
    conn = connect()
    rows = conn.execute(
        """SELECT utc_date, api_key_name,
                  SUM(CASE WHEN type='input_cache_hit_tokens' THEN amount ELSE 0 END)   AS cache_hit,
                  SUM(CASE WHEN type='input_cache_miss_tokens' THEN amount ELSE 0 END)   AS cache_miss,
                  SUM(CASE WHEN type='output_tokens' THEN amount ELSE 0 END)             AS output,
                  SUM(CASE WHEN type='request_count' THEN amount ELSE 0 END)             AS requests
           FROM amount_daily
           WHERE utc_date BETWEEN ? AND ? AND type != 'request_count'
           GROUP BY utc_date, api_key_name
           ORDER BY utc_date""",
        (start, end),
    ).fetchall()
    return [dict(r) for r in rows]


def get_model_totals(since: Optional[str] = None) -> list[dict]:
    """各模型汇总: tokens(输入/输出/缓存) 与 费用。"""
    conn = connect()
    params: tuple = ()
    sql = """SELECT model,
                    SUM(CASE WHEN type='input_cache_hit_tokens' THEN amount ELSE 0 END) AS cache_hit,
                    SUM(CASE WHEN type='input_cache_miss_tokens' THEN amount ELSE 0 END) AS cache_miss,
                    SUM(CASE WHEN type='output_tokens' THEN amount ELSE 0 END)           AS output
             FROM amount_daily
             WHERE type != 'request_count'"""
    if since:
        sql += " AND utc_date >= ?"
        params = (since,)
    sql += " GROUP BY model ORDER BY (cache_hit + cache_miss + output) DESC"
    rows = conn.execute(sql, params).fetchall()
    result = [dict(r) for r in rows]

    cost_rows = conn.execute(
        """SELECT model, currency, SUM(cost) AS cost
           FROM cost_daily GROUP BY model, currency""",
    ).fetchall()
    cost_map: dict[str, dict[str, float]] = {}
    for r in cost_rows:
        cost_map.setdefault(r["model"], {})[r["currency"]] = r["cost"]

    for item in result:
        item["cost"] = cost_map.get(item["model"], {})
        item["tokens"] = item["cache_hit"] + item["cache_miss"] + item["output"]
    return result


def get_daily_totals(start: str, end: str, metric: str = "tokens") -> list[dict]:
    """每日汇总序列(热力图数据)。metric: tokens | cost。"""
    conn = connect()
    if metric == "cost":
        rows = conn.execute(
            """SELECT utc_date, currency, SUM(cost) AS total
               FROM cost_daily
               WHERE utc_date BETWEEN ? AND ?
               GROUP BY utc_date, currency ORDER BY utc_date""",
            (start, end),
        ).fetchall()
        return [dict(r) for r in rows]
    rows = conn.execute(
        """SELECT utc_date,
                  SUM(CASE WHEN type='input_cache_hit_tokens' THEN amount ELSE 0 END) AS cache_hit,
                  SUM(CASE WHEN type='input_cache_miss_tokens' THEN amount ELSE 0 END) AS cache_miss,
                  SUM(CASE WHEN type='output_tokens' THEN amount ELSE 0 END)           AS output
           FROM amount_daily
           WHERE utc_date BETWEEN ? AND ? AND type != 'request_count'
           GROUP BY utc_date ORDER BY utc_date""",
        (start, end),
    ).fetchall()
    return [dict(r) for r in rows]


def get_cumulative(metric: str = "tokens") -> list[dict]:
    """累计曲线: 按日期升序的每日新增与累计值。"""
    conn = connect()
    if metric == "cost":
        rows = conn.execute(
            """SELECT utc_date, currency, SUM(cost) AS daily
               FROM cost_daily GROUP BY utc_date, currency ORDER BY utc_date"""
        ).fetchall()
        acc: dict[str, float] = {}
        out = []
        for r in rows:
            acc[r["currency"]] = acc.get(r["currency"], 0.0) + r["daily"]
            out.append({"utc_date": r["utc_date"], "currency": r["currency"],
                        "daily": r["daily"], "total": acc[r["currency"]]})
        return out
    rows = conn.execute(
        """SELECT utc_date,
                  SUM(CASE WHEN type='input_cache_hit_tokens' THEN amount ELSE 0 END) AS cache_hit,
                  SUM(CASE WHEN type='input_cache_miss_tokens' THEN amount ELSE 0 END) AS cache_miss,
                  SUM(CASE WHEN type='output_tokens' THEN amount ELSE 0 END)           AS output
           FROM amount_daily
           WHERE type != 'request_count'
           GROUP BY utc_date ORDER BY utc_date"""
    ).fetchall()
    acc_t = 0
    out = []
    for r in rows:
        daily = r["cache_hit"] + r["cache_miss"] + r["output"]
        acc_t += daily
        out.append({"utc_date": r["utc_date"], "daily": daily, "total": acc_t,
                    "cache_hit": r["cache_hit"], "cache_miss": r["cache_miss"],
                    "output": r["output"]})
    return out


# ---------- 分时(小时级)查询 ----------

def _hourly_rows_to_hours(usage_rows, cost_rows) -> dict:
    """分时行 → {hour: {cache_hit, cache_miss, output, requests, cost: {currency: v}}}。

    补全 0-23 全部小时(无数据小时全 0)。prompt_tokens 等冗余类型忽略
    (统计口径与日级一致: tokens = hit + miss + output)。
    """
    hours = {h: {"cache_hit": 0, "cache_miss": 0, "output": 0, "requests": 0, "cost": {}}
             for h in range(24)}
    for r in usage_rows:
        h = hours[r["hour"]]
        if r["type"] == "input_cache_hit_tokens":
            h["cache_hit"] += r["amount"]
        elif r["type"] == "input_cache_miss_tokens":
            h["cache_miss"] += r["amount"]
        elif r["type"] == "output_tokens":
            h["output"] += r["amount"]
        elif r["type"] == "request_count":
            h["requests"] += r["amount"]
    for r in cost_rows:
        h = hours[r["hour"]]
        h["cost"][r["currency"]] = round(h["cost"].get(r["currency"], 0.0) + r["cost"], 6)
    return hours


def get_hourly_detail(day: str) -> dict:
    """某日(GMT+8 本地日)的 24 小时分时明细(弹窗用), 返回 {date, hours:[24 项]}。"""
    conn = connect()
    usage = conn.execute(
        """SELECT hour, type, SUM(amount) AS amount FROM hourly_usage
           WHERE utc_date = ? GROUP BY hour, type""",
        (day,),
    ).fetchall()
    cost = conn.execute(
        """SELECT hour, currency, SUM(cost) AS cost FROM hourly_cost
           WHERE utc_date = ? GROUP BY hour, currency""",
        (day,),
    ).fetchall()
    hours = _hourly_rows_to_hours(usage, cost)
    return {"date": day, "hours": [dict(hour=h, **hours[h]) for h in range(24)]}


def get_hourly_aggregate(start: str, end: str) -> dict:
    """范围内所有日按小时聚合(分时面板用), 返回 {start, end, hours:[24 项]}。"""
    conn = connect()
    usage = conn.execute(
        """SELECT hour, type, SUM(amount) AS amount FROM hourly_usage
           WHERE utc_date BETWEEN ? AND ? GROUP BY hour, type""",
        (start, end),
    ).fetchall()
    cost = conn.execute(
        """SELECT hour, currency, SUM(cost) AS cost FROM hourly_cost
           WHERE utc_date BETWEEN ? AND ? GROUP BY hour, currency""",
        (start, end),
    ).fetchall()
    hours = _hourly_rows_to_hours(usage, cost)
    return {"start": start, "end": end,
            "hours": [dict(hour=h, **hours[h]) for h in range(24)]}


def get_hourly_detail_grouped(day: str, group: str) -> dict:
    """某日的 24 小时分时, 按模型或 API Key 拆分(弹窗切换视图用)。

    group: "model" | "key"。返回 {date, group, groups:[名称], hours:[24 项]},
    每项 {hour, requests(总量), reqValues:{名称: 请求数}, cost:{currency: v},
          costValues:{名称: {currency: 金额}}, values:{名称: tokens}}。
    """
    dim = "model" if group == "model" else "api_key_name"
    conn = connect()
    usage = conn.execute(
        f"""SELECT hour, {dim} AS dim,
                   SUM(CASE WHEN type='input_cache_hit_tokens' THEN amount ELSE 0 END) AS cache_hit,
                   SUM(CASE WHEN type='input_cache_miss_tokens' THEN amount ELSE 0 END) AS cache_miss,
                   SUM(CASE WHEN type='output_tokens' THEN amount ELSE 0 END)           AS output,
                   SUM(CASE WHEN type='request_count' THEN amount ELSE 0 END)           AS requests
            FROM hourly_usage WHERE utc_date = ?
            GROUP BY hour, {dim} ORDER BY hour""",
        (day,),
    ).fetchall()
    cost = conn.execute(
        f"""SELECT hour, {dim} AS dim, currency, SUM(cost) AS cost
            FROM hourly_cost WHERE utc_date = ?
            GROUP BY hour, {dim}, currency ORDER BY hour""",
        (day,),
    ).fetchall()

    hours = {h: {"hour": h, "requests": 0, "reqValues": {}, "cost": {},
                 "costValues": {}, "values": {}}
             for h in range(24)}
    groups: list[str] = []
    for r in usage:
        h = hours[r["hour"]]
        g = str(r["dim"] or "(未命名)")
        if g not in h["values"]:
            if g not in groups:
                groups.append(g)
        h["values"][g] = r["cache_hit"] + r["cache_miss"] + r["output"]
        h["reqValues"][g] = r["requests"]
        h["requests"] += r["requests"]
    for r in cost:
        h = hours[r["hour"]]
        h["cost"][r["currency"]] = round(h["cost"].get(r["currency"], 0.0) + r["cost"], 6)
        per_group = h["costValues"].setdefault(str(r["dim"] or "(未命名)"), {})
        per_group[r["currency"]] = round(per_group.get(r["currency"], 0.0) + r["cost"], 6)
    return {"date": day, "group": group, "groups": groups,
            "hours": [hours[h] for h in range(24)]}
