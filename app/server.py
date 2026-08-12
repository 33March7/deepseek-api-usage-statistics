"""FastAPI 应用: 统计查询 + 同步/登录控制 + 前端静态资源。

所有接口同源服务于 pywebview 内嵌窗口, 无需 CORS。
"""
from __future__ import annotations

import threading
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, sync
from .config import WEB_DIR, ensure_dirs

# 由 main.py 通过 env 注入: "mock" | "live"
MODE = "live"

app = FastAPI(title="DeepSeek 用量统计")

# 同步任务状态(仅内存, 单实例足够)
_sync_lock = threading.Lock()
_sync_state: dict = {"running": False, "last": None, "error": None}

# 登录窗口回调(由桌面壳调用, 也可被前端轮询 /api/auth/status)
_login_callback = None  # callable(token: str) -> None


def set_login_callback(fn) -> None:
    global _login_callback
    _login_callback = fn


def set_mode(mode: str) -> None:
    global MODE
    MODE = mode
    db.set_mode(mode)   # 同步切换数据库(实时主库 / mock 演示库)


# ---------- 认证 ----------

@app.get("/api/auth/status")
def auth_status():
    if MODE == "mock":
        return {"configured": True, "mode": "mock"}
    return {"configured": sync.has_token(), "mode": "live"}


class TokenIn(BaseModel):
    token: str


@app.post("/api/token")
def save_token(body: TokenIn):
    """保存登录凭证。先验证, 通过后才落盘; 无效凭证返回 400 且不动已保存凭证。"""
    token = (body.token or "").strip()
    if len(token) < 10:
        raise HTTPException(status_code=400, detail="凭证格式不正确, 请完整粘贴")
    result = sync.validate_token(token)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["message"])
    sync.save_token(token)
    # 验证成功: 顺便缓存最新余额快照
    if isinstance(result.get("summary"), dict):
        db.set_meta("user_summary", result["summary"])
    return {"ok": True, "valid": True, "message": "凭证有效"}


@app.post("/api/logout")
def logout():
    """退出登录: 清除本地凭证与缓存的余额快照(本地历史数据保留)。"""
    sync.clear_token()
    db.set_meta("user_summary", "")
    return {"ok": True, "message": "已退出登录"}


@app.post("/api/data/clear")
def clear_data():
    """清空全部本地数据(用量/费用/同步日志/统计缓存), 保留登录凭证。"""
    conn = db.connect()
    n1 = conn.execute("DELETE FROM amount_daily").rowcount
    n2 = conn.execute("DELETE FROM cost_daily").rowcount
    n3 = conn.execute("DELETE FROM sync_log").rowcount
    n4 = conn.execute("DELETE FROM hourly_usage").rowcount
    n5 = conn.execute("DELETE FROM hourly_cost").rowcount
    for key in ("last_sync_at", "earliest_date", "latest_date",
                "user_summary", "account_fingerprint", "account_keys"):
        conn.execute("DELETE FROM meta WHERE key=?", (key,))
    conn.commit()
    return {"ok": True, "message": f"已清空: 用量 {n1} 行, 费用 {n2} 行, 同步日志 {n3} 条, 分时 {n4+n5} 行"}


# ---------- 数据备份(导出/导入) ----------

# 导出文件结构版本; 修改导出格式时递增
BACKUP_VERSION = 1
# 各表导入时必需的字段
_BACKUP_REQUIRED = {
    "amount_daily": {"utc_date", "model", "api_key_name", "type", "amount"},
    "cost_daily": {"utc_date", "model", "api_key_name", "wallet_type", "cost", "currency"},
    "hourly_usage": {"utc_date", "hour", "model", "api_key_name", "type", "amount"},
    "hourly_cost": {"utc_date", "hour", "model", "api_key_name", "cost", "currency"},
}


def _export_payload() -> dict:
    """打包全部本地数据(不含登录凭证), 供手动导出与同步前自动备份共用。"""
    conn = db.connect()

    def dump(sql: str) -> list[dict]:
        return [dict(r) for r in conn.execute(sql).fetchall()]

    return {
        "app": "deepseek-usage-stats",
        "version": BACKUP_VERSION,
        "exported_at": db.now_iso(),
        "amount_daily": dump("SELECT * FROM amount_daily"),
        "cost_daily": dump("SELECT * FROM cost_daily"),
        "hourly_usage": dump("SELECT * FROM hourly_usage"),
        "hourly_cost": dump("SELECT * FROM hourly_cost"),
        "meta": {k: db.get_meta(k) for k in
                 ("last_sync_at", "earliest_date", "latest_date", "user_summary")},
    }


# 特殊存档点: 距今天 N 天的备份各保留一份(超出最近 BACKUP_KEEP 份限制, 随日期滚动逐步替换)
KEEP_DAYS = (2, 7, 14, 29)


def _backup_sort_key(p: Path):
    """备份文件名 → (日期, 时分秒, 微秒) 结构化排序键。

    新格式: backup-YYYYMMDD-HHMMSS-ffffff.json(微秒, 单调递增不回退);
    兼容旧格式 backup-YYYYMMDD-HHMMSS(.json | -N.json | -000000.json)。
    """
    import re
    m = re.match(r"backup-(\d{8})-(\d{6})(?:-(\d+))?\.json", p.name)
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


# 额外保留: 最近 KEEP_DAILY_DAYS 天每天最新的备份(不受 BACKUP_KEEP 份数上限限制)
from .config import KEEP_DAILY_DAYS  # noqa: E402


def _prune_backups() -> None:
    """清理自动备份: 保留最近 BACKUP_KEEP 份 + 最近 KEEP_DAILY_DAYS 天每天最新一份, 其余删除。"""
    import re
    from datetime import date as _date, timedelta as _td
    from .config import BACKUP_DIR, BACKUP_KEEP

    files = sorted(BACKUP_DIR.glob("backup-*.json"), key=_backup_sort_key)
    if not files:
        return
    protected = set(files[-BACKUP_KEEP:])          # 最近 N 份(文件级)
    by_day: dict[str, Path] = {}
    for p in files:                                # 按时间序覆盖 → 每天最新一份
        m = re.match(r"backup-(\d{8})-", p.name)
        by_day[m.group(1) if m else p.name] = p
    today = _date.today()
    for i in range(KEEP_DAILY_DAYS):
        day = (today - _td(days=i)).strftime("%Y%m%d")
        if day in by_day:
            protected.add(by_day[day])
    for p in files:
        if p not in protected:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass


def _save_auto_backup() -> str:
    """同步前自动备份当前数据到 data/backup/。

    保留策略: 最近 BACKUP_KEEP 份 + 最近 KEEP_DAILY_DAYS 天每天最新一份。
    文件名 backup-YYYYMMDD-HHMMSS-ffffff.json 用微秒时间戳, 单调递增不回退
    (序号方案在清理后会产生空洞导致序号复用, 新备份被误删)。返回文件名。
    """
    from .config import BACKUP_DIR
    ensure_dirs()
    from datetime import datetime as _dt
    name = f"backup-{_dt.now().strftime('%Y%m%d-%H%M%S-%f')}.json"
    import json as _json
    (BACKUP_DIR / name).write_text(
        _json.dumps(_export_payload(), ensure_ascii=False), encoding="utf-8")
    _prune_backups()
    return name


@app.get("/api/data/export")
def export_data():
    """导出全部本地数据为 JSON 备份(不含登录凭证)。"""
    return _export_payload()


def _check_backup_rows(rows: object, table: str) -> None:
    """校验某表的行列表结构与必需字段; 不通过抛 400(不落库)。"""
    if not isinstance(rows, list):
        raise HTTPException(status_code=400, detail=f"备份文件 {table} 结构不正确")
    required = _BACKUP_REQUIRED[table]
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            raise HTTPException(status_code=400,
                                detail=f"备份文件 {table} 第 {i + 1} 行不是对象")
        missing = required - set(r)
        if missing:
            raise HTTPException(status_code=400,
                                detail=f"备份文件 {table} 第 {i + 1} 行缺少字段: "
                                       f"{', '.join(sorted(missing))}")


@app.post("/api/data/import")
def import_data(body: dict = Body(...)):
    """导入备份: 校验通过后清空现有数据并恢复(全量替换语义)。

    校验不通过时不改动任何数据; 登录凭证不受影响。
    """
    if not isinstance(body, dict) or body.get("app") != "deepseek-usage-stats":
        raise HTTPException(status_code=400, detail="不是本工具的导出文件(app 标识不符)")
    if body.get("version") != BACKUP_VERSION:
        raise HTTPException(status_code=400, detail=f"备份文件版本不支持(期望 v{BACKUP_VERSION})")

    amount = body.get("amount_daily") or []
    cost = body.get("cost_daily") or []
    hourly_u = body.get("hourly_usage") or []
    hourly_c = body.get("hourly_cost") or []
    for table, rows in (("amount_daily", amount), ("cost_daily", cost),
                        ("hourly_usage", hourly_u), ("hourly_cost", hourly_c)):
        _check_backup_rows(rows, table)

    # 校验全部通过: 清空现有数据并写入
    conn = db.connect()
    conn.execute("DELETE FROM amount_daily")
    conn.execute("DELETE FROM cost_daily")
    conn.execute("DELETE FROM hourly_usage")
    conn.execute("DELETE FROM hourly_cost")
    for key in ("last_sync_at", "earliest_date", "latest_date",
                "user_summary", "account_fingerprint", "account_keys"):
        conn.execute("DELETE FROM meta WHERE key=?", (key,))
    conn.commit()

    n1 = db.upsert_amounts(amount)
    n2 = db.upsert_costs(cost)
    n3 = db.upsert_hourly_amounts(hourly_u)
    n4 = db.upsert_hourly_costs(hourly_c)
    meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
    for key in ("last_sync_at", "earliest_date", "latest_date", "user_summary"):
        if meta.get(key) not in (None, ""):
            db.set_meta(key, meta[key])
    db.log_sync_end(db.log_sync_start(), "ok",
                    f"[导入] 从备份恢复: 用量 {n1} 行, 费用 {n2} 行, 分时 {n3 + n4} 行")
    return {"ok": True,
            "message": f"导入完成: 用量 {n1} 行, 费用 {n2} 行, 分时 {n3 + n4} 行"}


# ---------- 同步 ----------

def _run_sync_async():
    global _sync_state
    try:
        result = sync.run_sync()
        _sync_state = {"running": False, "last": result, "error": None}
    except sync.SyncError as e:
        _sync_state = {"running": False, "last": None,
                       "error": {"message": str(e), "expired": e.expired,
                                 "account_changed": getattr(e, "account_changed", False)}}
    except Exception as e:
        _sync_state = {"running": False, "last": None, "error": {"message": str(e), "expired": False}}


@app.post("/api/login/start")
def login_start():
    """打开桌面登录窗口(由 pywebview 壳实现); 浏览器模式回退为新页面。"""
    if MODE == "mock":
        return {"ok": True, "message": "mock 模式无需登录"}
    if _login_callback:
        _login_callback()
        return {"ok": True, "message": "已打开登录窗口"}
    # 无桌面壳(如直接浏览器访问): 引导去平台复制 token
    return {"ok": True, "message": "请在浏览器中打开平台并粘贴 token", "manual": True}


@app.post("/api/sync")
def trigger_sync():
    global _sync_state
    if MODE == "mock":
        # 演示数据已存在时不覆盖(保护定制的演示数据)
        if db.earliest_date() is None:
            from . import mock_data
            mock_data.generate()
            return {"ok": True, "message": "已生成演示数据(mock 模式)"}
        return {"ok": True, "message": "演示模式: 数据已就绪, 无需同步"}
    if not sync.has_token():
        raise HTTPException(status_code=400, detail="尚未配置登录, 请先登录")
    with _sync_lock:
        if _sync_state["running"]:
            raise HTTPException(status_code=409, detail="同步正在进行中")
        _sync_state = {"running": True, "last": None, "error": None}
    # 同步前自动备份当前数据(data/backup/, 最多保留 20 份); 失败不阻断同步
    backup_note = ""
    try:
        name = _save_auto_backup()
        backup_note = f"; 已自动备份: data/backup/{name}"
    except Exception as e:
        backup_note = f"; 自动备份失败({e})"
    threading.Thread(target=_run_sync_async, daemon=True).start()
    return {"ok": True, "message": f"同步已开始{backup_note}"}


@app.get("/api/sync/status")
def sync_status():
    return {
        "running": _sync_state["running"],
        "last": _sync_state["last"],
        "error": _sync_state["error"],
    }


@app.get("/api/sync/logs")
def sync_logs(limit: int = Query(20, ge=1, le=100)):
    return db.get_sync_logs(limit)


# ---------- 统计 ----------

def _date_range(days: int) -> tuple[str, str]:
    end = date.today()
    if days <= 0:
        # "全部": 从最早有数据的一天到今天
        earliest = db.earliest_date()
        start = date.fromisoformat(earliest) if earliest else end
    else:
        start = end - timedelta(days=days - 1)
    return start.isoformat(), end.isoformat()


@app.get("/api/stats/summary")
def stats_summary():
    # 数据按 SYNC_TZ(默认 GMT+8)的本地日存储, "今日/本月"直接用该时区的今天
    from .config import SYNC_TZ
    today = datetime.now(SYNC_TZ).date().isoformat()
    month_start = today[:8] + "01"

    amounts = db.get_amount_totals()
    month_amounts = db.get_amount_totals(since=month_start)
    today_amounts = db.get_amount_totals(since=today)

    def tokens_of(m: dict) -> int:
        return int(m.get("input_cache_hit_tokens", 0)
                   + m.get("input_cache_miss_tokens", 0)
                   + m.get("output_tokens", 0))

    costs = db.get_cost_totals()
    month_costs = db.get_cost_totals(since=month_start)
    today_costs = db.get_cost_totals(since=today)

    def cost_by_currency(c: dict) -> dict[str, float]:
        out: dict[str, float] = {}
        for (currency, _wallet), v in c.items():
            out[currency] = round(out.get(currency, 0.0) + v, 6)
        return out

    summary = _load_summary()
    return {
        "total": {
            "tokens": tokens_of(amounts),
            "cache_hit": int(amounts.get("input_cache_hit_tokens", 0)),
            "cache_miss": int(amounts.get("input_cache_miss_tokens", 0)),
            "output": int(amounts.get("output_tokens", 0)),
            "requests": int(amounts.get("request_count", 0)),
            "cost": cost_by_currency(costs),
        },
        "month": {
            "tokens": tokens_of(month_amounts),
            "requests": int(month_amounts.get("request_count", 0)),
            "cost": cost_by_currency(month_costs),
        },
        "today": {
            "tokens": tokens_of(today_amounts),
            "requests": int(today_amounts.get("request_count", 0)),
            "cost": cost_by_currency(today_costs),
        },
        "dates": {
            "earliest": db.earliest_date(),
            "latest": db.latest_date(),
            "last_sync_at": db.get_meta("last_sync_at"),
        },
        "balance": _extract_balance(summary),
    }


def _load_summary() -> Optional[dict]:
    """读取缓存的 user_summary(meta 中为 JSON 字符串)。"""
    import json as _json
    raw = db.get_meta("user_summary")
    if isinstance(raw, str) and raw:
        try:
            return _json.loads(raw)
        except ValueError:
            return None
    return raw if isinstance(raw, dict) else None


def _extract_balance(summary: Optional[dict]) -> Optional[dict]:
    """从 user_summary 的 biz_data 提取余额(真实格式, 2026-08 校准):

    {normal_wallets: [{currency, balance, token_estimation}],
     bonus_wallets: [...], total_costs: [{currency, amount}]}
    """
    if not isinstance(summary, dict):
        return None
    normal = summary.get("normal_wallets") or []
    bonus = summary.get("bonus_wallets") or []
    costs = summary.get("total_costs") or []
    if not normal and not bonus:
        return None

    # 按币种合并: 总余额 / 赠送余额 / 累计花费
    merged: dict[str, dict] = {}
    for w in normal:
        c = str(w.get("currency") or "CNY")
        merged.setdefault(c, {"total": 0.0, "bonus": 0.0})
        merged[c]["total"] += _to_num(w.get("balance", 0))
    for w in bonus:
        c = str(w.get("currency") or "CNY")
        merged.setdefault(c, {"total": 0.0, "bonus": 0.0})
        merged[c]["total"] += _to_num(w.get("balance", 0))
        merged[c]["bonus"] += _to_num(w.get("balance", 0))
    cost_map = {str(x.get("currency") or "CNY"): _to_num(x.get("amount", 0)) for x in costs}

    return {
        "wallets": [{"currency": c, "total": v["total"], "bonus": v["bonus"]}
                    for c, v in merged.items()],
        "total_cost": [{"currency": c, "amount": a} for c, a in cost_map.items()],
    }


def _to_num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


@app.get("/api/stats/daily")
def stats_daily(days: int = Query(90, ge=0, le=3650),
                group: str = Query("model", pattern="^(model|key)$")):
    """每日用量: 按模型/API Key 堆叠(含缓存命中/未命中/输出拆分)。days=0 表示全部历史。"""
    start, end = _date_range(days)
    rows = db.get_daily_by_key(start, end) if group == "key" else db.get_daily_by_model(start, end)
    return {"start": start, "end": end, "group": group, "rows": rows}


@app.get("/api/stats/models")
def stats_models():
    return db.get_model_totals()


@app.get("/api/stats/heatmap")
def stats_heatmap(start: str, end: str, metric: str = Query("tokens", pattern="^(tokens|cost)$")):
    """GitHub 风格热力图数据: 日期 → 数值。"""
    rows = db.get_daily_totals(start, end, metric)
    return {"start": start, "end": end, "metric": metric, "rows": rows}


@app.get("/api/stats/cumulative")
def stats_cumulative(metric: str = Query("tokens", pattern="^(tokens|cost)$")):
    return db.get_cumulative(metric)


# ---------- 分时(小时级)统计 ----------

@app.get("/api/stats/hourly/detail")
def stats_hourly_detail(date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
                        group: str = Query("type", pattern="^(type|model|key)$")):
    """某日(GMT+8 本地日)的 24 小时分时明细(点击柱状图弹窗用)。

    group=type: 总量 + 计费类型拆分(默认); group=model|key: 按模型/API Key 拆分。
    """
    if group == "type":
        return db.get_hourly_detail(date)
    return db.get_hourly_detail_grouped(date, group)


@app.get("/api/stats/hourly/aggregate")
def stats_hourly_aggregate(days: int = Query(30, ge=1, le=365)):
    """范围内所有日按小时聚合(分时面板: 同时段加总)。days 按 SYNC_TZ 今天回溯。"""
    from .config import SYNC_TZ
    end = datetime.now(SYNC_TZ).date()
    start = end - timedelta(days=days - 1)
    return db.get_hourly_aggregate(start.isoformat(), end.isoformat())


# ---------- 静态资源 ----------

@app.get("/")
def index():
    ensure_dirs()
    return FileResponse(WEB_DIR / "index.html")


app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")


# ---------- 异常兜底 ----------

@app.exception_handler(HTTPException)
async def http_exc_handler(request, exc):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(Exception)
async def generic_exc_handler(request, exc):
    return JSONResponse(status_code=500, content={"detail": f"服务器内部错误: {exc}"})
