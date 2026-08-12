"""DeepSeek 平台数据同步引擎。

数据来源: platform.deepseek.com 私有 Dashboard 接口(需浏览器登录会话的
userToken, 以 Authorization: Bearer <token> 认证)。

2026-08 实测校准(与平台「用量信息」页面完全一致):
  主接口(按时间范围 + 时区分桶, 平台页面实际使用):
    GET /api/v0/usage/by_api_key/amount?start=<unix秒>&end=<unix秒>&tz=<偏移秒>
    GET /api/v0/usage/by_api_key/cost?start=<unix秒>&end=<unix秒>&tz=<偏移秒>
    响应: {code, msg, data:{biz_code, biz_msg, biz_data:{
            start, end, bucket:86400, models:[...],
            amount: series:[{api_key:{tracking_id,name,sensitive_id,valid},
                             model, buckets:[{time, usage:{PROMPT_CACHE_HIT_TOKEN,
                             PROMPT_CACHE_MISS_TOKEN, RESPONSE_TOKEN, REQUEST}}]}]
            cost: data:[{currency, series:[{api_key, model,
                             buckets:[{time, cost:"0.123"}]}]}]}}}
    约束: start/end 必须按 tz 的日边界对齐; 单次范围上限 30 天(31 天返回
          INVALID_PARAM); 空区间返回空 series。
  tz 参数 = 时区偏移秒(如 GMT+8 = 28800), 日桶按该时区切分。

数据日期 = 按 SYNC_TZ(默认 GMT+8)的本地日, 与平台页面显示一致。
同步以 30 天窗口从今天往前回溯, 连续 2 个全零窗口停止。

注意: 私有接口无文档、可能变动。本模块是唯一对接点, 未知结构时保存原始
响应到 data/raw/ 便于校准, 并抛出带提示的错误。
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timezone, timedelta
from typing import Any, Optional
from urllib.parse import urlencode

import requests

from . import db
from .config import (
    API_BASE,
    BY_KEY_MAX_RANGE_DAYS,
    HTTP_TIMEOUT,
    MAX_BACKFILL_MONTHS,
    RAW_DIR,
    REQUEST_DELAY,
    SESSION_EXPIRED_CODES,
    SYNC_TZ,
    SYNC_TZ_OFFSET_SEC,
    TOKEN_PATH,
    USAGE_BY_KEY_AMOUNT_URL,
    USAGE_BY_KEY_COST_URL,
    USER_SUMMARY_URL,
    ensure_dirs,
)

# 平台计费类型枚举 → 数据库统一小写类型
PLATFORM_TYPE_MAP = {
    "PROMPT_TOKEN": "prompt_tokens",
    "PROMPT_CACHE_HIT_TOKEN": "input_cache_hit_tokens",
    "PROMPT_CACHE_MISS_TOKEN": "input_cache_miss_tokens",
    "RESPONSE_TOKEN": "output_tokens",
    "REQUEST": "request_count",
}


class SyncError(Exception):
    """同步失败, message 面向用户展示。"""

    def __init__(self, message: str, expired: bool = False, account_changed: bool = False):
        super().__init__(message)
        self.expired = expired
        self.account_changed = account_changed


# ---------- token 存取 ----------

def save_token(token: str) -> None:
    token = (token or "").strip()
    # 去掉可能的 "Bearer " 前缀
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    ensure_dirs()
    with open(TOKEN_PATH, "w", encoding="utf-8") as f:
        json.dump({"user_token": token, "saved_at": db.now_iso()}, f, ensure_ascii=False, indent=2)


def load_token() -> Optional[str]:
    try:
        with open(TOKEN_PATH, encoding="utf-8") as f:
            data = json.load(f)
        token = (data.get("user_token") or "").strip()
        return token or None
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return None


def has_token() -> bool:
    return bool(load_token())


def clear_token() -> None:
    """清除本地登录凭证(退出登录)。"""
    try:
        TOKEN_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def validate_token(token: Optional[str] = None) -> dict:
    """验证 token 有效性(不修改已保存凭证)。

    参数 token 为空时使用已保存的凭证。返回 {ok, message, summary}。
    """
    candidate = (token or "").strip() or load_token()
    if not candidate:
        return {"ok": False, "message": "尚未配置登录凭证", "summary": None}
    client = PlatformClient(candidate)
    try:
        summary = client.get_user_summary()
        return {"ok": True, "message": "凭证有效", "summary": summary}
    except SyncError as e:
        return {"ok": False, "message": str(e), "summary": None}


# ---------- 平台客户端 ----------

class PlatformClient:
    """封装 platform.deepseek.com 私有接口的调用与原始响应存档。"""

    def __init__(self, token: str):
        self.token = token
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        })

    def _get(self, url: str, params: dict) -> Any:
        """GET 并解析 JSON; 校验会话有效性; 原始响应存档。"""
        try:
            resp = self.session.get(url, params=params, timeout=HTTP_TIMEOUT)
        except requests.RequestException as e:
            raise SyncError(f"网络请求失败: {e}") from e

        # 存档原始响应(供接口结构变动时排查)
        try:
            ensure_dirs()
            name = url.split("/")[-1].split("?")[0]
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            with open(RAW_DIR / f"{name}_{stamp}.json", "w", encoding="utf-8") as f:
                f.write(f"{url}?{urlencode(params)}\n")
                f.write(resp.text)
        except OSError:
            pass  # 存档失败不影响主流程

        try:
            data = resp.json()
        except ValueError:
            raise SyncError(f"接口返回非 JSON(HTTP {resp.status_code}), 已存档到 data/raw/ 便于排查") from None

        # 会话过期/无权限: 平台错误码形如 {"code": "40002", ...}
        if isinstance(data, dict):
            code = str(data.get("code", ""))
            if code in SESSION_EXPIRED_CODES or resp.status_code in (401, 403):
                raise SyncError("登录已过期或无效, 请重新登录", expired=True)
            if code and code not in ("0", "200", "success"):
                raise SyncError(f"平台接口错误(code={code}): {data.get('message', data.get('msg', ''))}")

        # 业务错误(HTTP 200 但 biz_code != 0)
        biz = data.get("data") if isinstance(data, dict) else None
        if isinstance(biz, dict):
            biz_code = str(biz.get("biz_code", "0"))
            if biz_code not in ("0", "200"):
                raise SyncError(f"平台接口错误: {biz.get('biz_msg') or biz_code}")

        return data

    def get_user_summary(self) -> Optional[dict]:
        """用户汇总(余额/累计花费)。"""
        data = self._get(USER_SUMMARY_URL, {})
        payload = _biz_payload(data)
        return payload if isinstance(payload, dict) else None

    def fetch_amount_window(self, start_sec: int, end_sec: int) -> tuple[list[dict], str, frozenset[str]]:
        """拉取 [start, end) 的用量明细(日桶, 按 SYNC_TZ 对齐)。

        返回 (归一化行列表, 账号指纹, API Key 集合)。Key 集合用于检测
        "登录账号与本地数据所属账号"是否一致(见 _accounts_conflict)。
        """
        data = self._get(USAGE_BY_KEY_AMOUNT_URL,
                         {"start": start_sec, "end": end_sec, "tz": SYNC_TZ_OFFSET_SEC})
        rows = _normalize_amount_bykey(data)
        payload = _biz_payload(data)
        time.sleep(REQUEST_DELAY)
        return rows, _account_fingerprint(payload), _account_keys(payload)

    def fetch_cost_window(self, start_sec: int, end_sec: int) -> list[dict]:
        """拉取 [start, end) 的费用明细(日桶, 按 SYNC_TZ 对齐), 归一化为行列表。"""
        data = self._get(USAGE_BY_KEY_COST_URL,
                         {"start": start_sec, "end": end_sec, "tz": SYNC_TZ_OFFSET_SEC})
        rows = _normalize_cost_bykey(data)
        time.sleep(REQUEST_DELAY)
        return rows

    def fetch_balance(self) -> Optional[dict]:
        """官方公开余额接口(需 API Key, 通常不可用); 失败返回 None。"""
        try:
            resp = self.session.get(API_BASE + "/user/balance", timeout=HTTP_TIMEOUT)
            if resp.status_code == 200:
                return resp.json()
        except requests.RequestException:
            pass
        return None

    def fetch_hourly_amount_window(self, start_sec: int, end_sec: int) -> tuple[list[dict], str, frozenset[str]]:
        """拉取 [start, end) 的分时用量(bucket=3600, 按 SYNC_TZ 对齐)。

        窗口必须按日边界对齐(单日 24 桶/系列)。返回 (按小时聚合的行, 指纹, Key 集合)。
        """
        data = self._get(USAGE_BY_KEY_AMOUNT_URL, {
            "start": start_sec, "end": end_sec, "tz": SYNC_TZ_OFFSET_SEC, "bucket": 3600,
        })
        rows = _normalize_amount_hourly(data)
        payload = _biz_payload(data)
        time.sleep(REQUEST_DELAY)
        return rows, _account_fingerprint(payload), _account_keys(payload)

    def fetch_hourly_cost_window(self, start_sec: int, end_sec: int) -> list[dict]:
        """拉取 [start, end) 的分时费用(bucket=3600), 返回按小时聚合的行。"""
        data = self._get(USAGE_BY_KEY_COST_URL, {
            "start": start_sec, "end": end_sec, "tz": SYNC_TZ_OFFSET_SEC, "bucket": 3600,
        })
        rows = _normalize_cost_hourly(data)
        time.sleep(REQUEST_DELAY)
        return rows


# ---------- 归一化层 ----------

def _account_keys(biz_data: Any) -> frozenset[str]:
    """响应中的 API Key 集合(tracking_id|name)。无 Key 数据返回空集。"""
    if not isinstance(biz_data, dict):
        return frozenset()
    keys = set()
    for s in biz_data.get("series", []):
        if not isinstance(s, dict):
            continue
        ak = s.get("api_key") or {}
        tid = str(ak.get("tracking_id") or "")
        name = str(ak.get("name") or "")
        if tid or name:
            keys.add(f"{tid}|{name}")
    return frozenset(keys)


def _account_fingerprint(biz_data: Any) -> str:
    """账号指纹: 响应中 API Key 集合的哈希(供存档/展示, 一致性判定见 _accounts_conflict)。

    无 Key 数据时返回 "empty"(无法区分, 属可接受的局限)。
    """
    keys = _account_keys(biz_data)
    if not keys:
        return "empty"
    import hashlib
    return hashlib.sha256(";".join(sorted(keys)).encode()).hexdigest()[:16]


def _load_keys(raw: Any) -> frozenset[str]:
    """meta 中存储的 Key 集合(JSON list) → frozenset。"""
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(str(k) for k in raw)


def _accounts_conflict(stored: frozenset[str], current: frozenset[str]) -> bool:
    """本地已存 Key 集合与当前响应 Key 集合是否冲突(疑似不同账号)。

    同一账号的 Key 集合会随活跃度增减(某日无流量的 Key 可能不出现在响应中),
    因此只有"双方非空且完全不相交"才判为冲突; 有交集或任一方为空视为一致。
    """
    return bool(stored) and bool(current) and stored.isdisjoint(current)


def _biz_payload(data: Any) -> Any:
    """提取真实格式的负载: {code, msg, data:{biz_code, biz_msg, biz_data}} → biz_data。"""
    if isinstance(data, dict):
        inner = data.get("data")
        if isinstance(inner, dict):
            return inner.get("biz_data")
        return data.get("biz_data")
    return None


def _to_num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _to_int(v: Any) -> int:
    return int(_to_num(v))


def _map_type(type_: str) -> str:
    """平台枚举 → 数据库小写类型; 未知类型原样小写化。"""
    if not type_:
        return ""
    return PLATFORM_TYPE_MAP.get(type_.upper(), type_.lower())


def _ts_to_date(ts: Any) -> str:
    """桶时间戳(Unix 秒, 按 SYNC_TZ 对齐) → 本地日期字符串。"""
    try:
        return datetime.fromtimestamp(int(ts), SYNC_TZ).date().isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def _normalize_amount_bykey(data: Any) -> list[dict]:
    """by_api_key/amount 响应 → 行列表 (utc_date=本地日, model, api_key_name, type, amount)。

    全零行跳过(0 用量不影响任何统计)。
    """
    payload = _biz_payload(data)
    if not isinstance(payload, dict):
        return []
    rows: list[dict] = []
    for series in payload.get("series", []):
        if not isinstance(series, dict):
            continue
        key_info = series.get("api_key") or {}
        key_name = str(key_info.get("name") or "")
        model = str(series.get("model") or "unknown")
        for b in series.get("buckets", []):
            d = _ts_to_date(b.get("time"))
            if not d:
                continue
            usage = b.get("usage") or {}
            for type_, amount in usage.items():
                n = _to_int(amount)
                if n == 0:
                    continue
                rows.append({
                    "utc_date": d, "model": model, "api_key_name": key_name,
                    "type": _map_type(str(type_)), "amount": n, "price": None,
                })
    return rows


def _normalize_cost_bykey(data: Any) -> list[dict]:
    """by_api_key/cost 响应 → 行列表 (utc_date=本地日, model, api_key_name, cost, currency)。

    全零行跳过。
    """
    payload = _biz_payload(data)
    if not isinstance(payload, dict):
        return []
    rows: list[dict] = []
    for entry in payload.get("data", []):
        if not isinstance(entry, dict):
            continue
        currency = str(entry.get("currency") or "CNY")
        for series in entry.get("series", []):
            if not isinstance(series, dict):
                continue
            key_info = series.get("api_key") or {}
            key_name = str(key_info.get("name") or "")
            model = str(series.get("model") or "unknown")
            for b in series.get("buckets", []):
                cost = _to_num(b.get("cost", 0))
                if cost == 0:
                    continue
                d = _ts_to_date(b.get("time"))
                if not d:
                    continue
                rows.append({
                    "utc_date": d, "model": model, "api_key_name": key_name,
                    "wallet_type": "default", "cost": cost, "currency": currency,
                })
    return rows


# ---------- 分时(小时级)归一化 ----------
#
# 分时接口 = 同 by_api_key 接口 + bucket=3600 参数(2026-08 实测可用)。
# 响应结构与日级完全一致, 只是桶为整点小时。分时表按 (日期, 小时, 类型)
# 聚合, 不保留 model/api_key 维度(分时按总量展示)。

# 分时表只保留的核心计费类型(prompt_tokens 等冗余类型跳过, 避免重复计)
_HOURLY_TYPES = {
    "input_cache_hit_tokens", "input_cache_miss_tokens",
    "output_tokens", "request_count",
}


def _ts_to_hour(ts: Any) -> tuple[str, int]:
    """桶时间戳(Unix 秒, 按 SYNC_TZ 对齐) → (本地日期, 小时)。"""
    try:
        dt = datetime.fromtimestamp(int(ts), SYNC_TZ)
        return dt.date().isoformat(), dt.hour
    except (TypeError, ValueError, OSError):
        return "", -1


def _normalize_amount_hourly(data: Any) -> list[dict]:
    """分时 amount 响应 → 行列表 (utc_date, hour, model, api_key_name, type, amount)。

    保留模型/API Key 维度(弹窗支持按模型/Key 拆分)。全零跳过。
    """
    payload = _biz_payload(data)
    if not isinstance(payload, dict):
        return []
    rows: list[dict] = []
    for series in payload.get("series", []):
        if not isinstance(series, dict):
            continue
        key_info = series.get("api_key") or {}
        key_name = str(key_info.get("name") or "")
        model = str(series.get("model") or "unknown")
        for b in series.get("buckets", []):
            d, hour = _ts_to_hour(b.get("time"))
            if not d:
                continue
            usage = b.get("usage") or {}
            for type_, amount in usage.items():
                n = _to_int(amount)
                if n == 0:
                    continue
                t = _map_type(str(type_))
                if t not in _HOURLY_TYPES:
                    continue
                rows.append({
                    "utc_date": d, "hour": hour, "model": model,
                    "api_key_name": key_name, "type": t, "amount": n,
                })
    return rows


def _normalize_cost_hourly(data: Any) -> list[dict]:
    """分时 cost 响应 → 行列表 (utc_date, hour, model, api_key_name, cost, currency)。

    保留模型/API Key 维度。全零跳过。
    """
    payload = _biz_payload(data)
    if not isinstance(payload, dict):
        return []
    rows: list[dict] = []
    for entry in payload.get("data", []):
        if not isinstance(entry, dict):
            continue
        currency = str(entry.get("currency") or "CNY")
        for series in entry.get("series", []):
            if not isinstance(series, dict):
                continue
            key_info = series.get("api_key") or {}
            key_name = str(key_info.get("name") or "")
            model = str(series.get("model") or "unknown")
            for b in series.get("buckets", []):
                cost = _to_num(b.get("cost", 0))
                if cost == 0:
                    continue
                d, hour = _ts_to_hour(b.get("time"))
                if not d:
                    continue
                rows.append({
                    "utc_date": d, "hour": hour, "model": model,
                    "api_key_name": key_name, "cost": round(cost, 6), "currency": currency,
                })
    return rows


# ---------- 同步引擎 ----------

def _day_unix(d: date) -> int:
    """本地日 00:00 在 SYNC_TZ 下的 Unix 秒。"""
    return int(datetime(d.year, d.month, d.day, tzinfo=SYNC_TZ).timestamp())


def _window_iter(start: date):
    """以 30 天窗口从 start(含)往前回溯, 产出 (start_sec, end_sec) 日对齐区间。"""
    end_d = start + timedelta(days=1)  # end 为日边界(不含)
    for _ in range(MAX_BACKFILL_MONTHS * 2):
        begin_d = end_d - timedelta(days=BY_KEY_MAX_RANGE_DAYS)
        yield _day_unix(begin_d), _day_unix(end_d)
        if begin_d <= date(2020, 1, 1):
            break
        end_d = begin_d


def _backfill(client: PlatformClient, fetch_fn, upsert_fn) -> tuple[int, int, int]:
    """30 天窗口逐段回溯, 连续 2 个全零窗口即停止。

    返回 (有数据窗口数, 行数, 全零窗口计数)。
    """
    today = datetime.now(SYNC_TZ).date()
    windows = rows_total = zero_streak = 0
    for start_sec, end_sec in _window_iter(today):
        rows = fetch_fn(start_sec, end_sec)
        n = upsert_fn(rows)
        rows_total += n
        if not rows:
            zero_streak += 1
            if zero_streak >= 2:
                break
        else:
            zero_streak = 0
            windows += 1
    return windows, rows_total, zero_streak


def _sync_hourly(client: PlatformClient) -> dict:
    """抓取今天+昨天的分时(小时级)数据(平台仅保留 2 天小时数据)。

    在日级同步成功后调用; 失败由调用方降级(不影响日级结果)。
    返回 {days, rows_amount, rows_cost}。
    """
    today = datetime.now(SYNC_TZ).date()
    stored_keys = _load_keys(db.get_meta("account_keys"))
    rows_amount = rows_cost = 0
    for d in (today, today - timedelta(days=1)):
        start_sec, end_sec = _day_unix(d), _day_unix(d + timedelta(days=1))
        rows, _fingerprint, keys = client.fetch_hourly_amount_window(start_sec, end_sec)
        # 账号一致性: 响应 Key 集合与日级指纹完全不相交才判为切换(防混账号)。
        # 同一账号的 Key 集合会随活跃度增减, 有交集即视为同一账号。
        if _accounts_conflict(stored_keys, keys):
            raise SyncError("分时数据账号指纹与本地不一致, 已跳过(日级数据未受影响)")
        rows_amount += db.upsert_hourly_amounts(rows)
        rows_cost += db.upsert_hourly_costs(client.fetch_hourly_cost_window(start_sec, end_sec))
    return {"days": 2, "rows_amount": rows_amount, "rows_cost": rows_cost}


def run_sync() -> dict:
    """执行一次完整同步(手动触发): 30 天窗口回溯全部历史(按 GMT+8 本地日)。

    数据策略:
      - 增量 upsert: 同主键覆盖更新, 响应中缺失的旧行保留(平台删除久远数据
        不影响本地历史);
      - 账号一致性: 首个窗口先比对账号指纹, 与本地数据所属账号不一致时
        中止且不写入任何数据(抛 account_changed=True 的 SyncError)。

    返回 {status, message, months_amount, months_cost, ...}。
    """
    if not has_token():
        raise SyncError("尚未配置登录, 请先登录", expired=True)

    client = PlatformClient(load_token())
    log_id = None

    # 若存在 mock 演示数据标记: 真实同步前先清空, 避免演示数据混入真实统计
    if db.get_meta("mock_generated_at"):
        conn = db.connect()
        conn.execute("DELETE FROM amount_daily")
        conn.execute("DELETE FROM cost_daily")
        conn.execute("DELETE FROM sync_log")
        conn.execute("DELETE FROM hourly_usage")
        conn.execute("DELETE FROM hourly_cost")
        conn.execute("DELETE FROM meta WHERE key='mock_generated_at'")
        conn.commit()

    log_id = db.log_sync_start()

    windows_amount = windows_cost = rows_amount = rows_cost = 0
    zero_amount = zero_cost = 0
    try:
        # ---------- 用量: 回溯 + 账号一致性门禁 ----------
        today = datetime.now(SYNC_TZ).date()
        first_window = True
        zero_streak = 0
        for start_sec, end_sec in _window_iter(today):
            rows, fingerprint, keys = client.fetch_amount_window(start_sec, end_sec)
            if first_window:
                first_window = False
                stored_keys = _load_keys(db.get_meta("account_keys"))
                if _accounts_conflict(stored_keys, keys):
                    db.log_sync_end(log_id, "error",
                                    "账号变更: 登录账号与本地数据所属账号不一致, 已中止且未写入任何数据",
                                    windows_amount, windows_cost)
                    raise SyncError(
                        "检测到登录账号与本地数据不一致(API Key 集合不同)。"
                        "本地历史数据保留未动;如需切换到新账号,请选择「清空数据并同步新账号」。",
                        account_changed=True)
                db.set_meta("account_fingerprint", fingerprint)
                db.set_meta("account_keys", sorted(keys))
            n = db.upsert_amounts(rows)
            rows_amount += n
            if not rows:
                zero_streak += 1
                if zero_streak >= 2:
                    break
            else:
                zero_streak = 0
                windows_amount += 1

        # ---------- 费用: 同样回溯 ----------
        zero_streak = 0
        for start_sec, end_sec in _window_iter(today):
            rows = client.fetch_cost_window(start_sec, end_sec)
            n = db.upsert_costs(rows)
            rows_cost += n
            if not rows:
                zero_streak += 1
                if zero_streak >= 2:
                    break
            else:
                zero_streak = 0
                windows_cost += 1

        # 尽力保存余额快照与汇总信息
        try:
            summary = client.get_user_summary()
            if summary:
                db.set_meta("user_summary", summary)
        except SyncError:
            pass

        # 分时(小时级)数据: 仅今天+昨天。失败只降级, 不影响日级同步结果
        hourly_note = ""
        try:
            h = _sync_hourly(client)
            hourly_note = (f"; 分时 {h['days']} 天({h['rows_amount']} 行用量"
                           f"/{h['rows_cost']} 行费用)")
        except Exception as e:
            hourly_note = f"; 分时数据跳过({e})"

        msg = (f"用量回溯 {windows_amount} 个窗口({rows_amount} 行), "
               f"费用回溯 {windows_cost} 个窗口({rows_cost} 行); "
               f"旧数据保留未变{hourly_note}")
        db.log_sync_end(log_id, "ok", msg, windows_amount, windows_cost)
        db.set_meta("last_sync_at", db.now_iso())
        db.set_meta("earliest_date", db.earliest_date())
        db.set_meta("latest_date", db.latest_date())
        return {"status": "ok", "message": msg,
                "months_amount": windows_amount, "months_cost": windows_cost,
                "rows_amount": rows_amount, "rows_cost": rows_cost,
                "zero_streak": {"amount": zero_amount, "cost": zero_cost}}
    except SyncError as e:
        db.log_sync_end(log_id, "error", str(e), windows_amount, windows_cost)
        raise
    except Exception as e:  # 未知异常也要记录
        db.log_sync_end(log_id, "error", f"未知错误: {e}", windows_amount, windows_cost)
        raise SyncError(f"同步失败: {e}") from e
