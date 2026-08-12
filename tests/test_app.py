"""集成测试: mock 数据 → 全部统计端点 → 归一化层。

运行: py -m tests.test_app
"""
from __future__ import annotations

import sys
import threading
import time
from datetime import date, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db, mock_data, server, sync  # noqa: E402


def test_normalize():
    """归一化层: by_api_key 真实格式(2026-08 实测), 应按 GMT+8 本地日正确提取行。"""

    # --- by_api_key/amount: series[].buckets[].usage, tz=28800 对齐 ---
    raw1 = {"code": "0", "data": {"biz_code": "0", "biz_msg": "",
            "biz_data": {"start": 1786204800, "end": 1786550400, "bucket": 86400,
                "models": ["deepseek-v4-pro"], "series": [
                    {"api_key": {"tracking_id": "x", "name": "Zcode2",
                                 "sensitive_id": "sk-***", "valid": True},
                     "model": "deepseek-v4-pro",
                     "buckets": [
                         {"time": 1786464000, "usage": {
                             "PROMPT_CACHE_HIT_TOKEN": "18119168",
                             "PROMPT_CACHE_MISS_TOKEN": "212592",
                             "RESPONSE_TOKEN": "187968",
                             "REQUEST": "248"}},
                         {"time": 1786377600, "usage": {
                             "PROMPT_CACHE_HIT_TOKEN": "0",
                             "PROMPT_CACHE_MISS_TOKEN": "0",
                             "RESPONSE_TOKEN": "0",
                             "REQUEST": "0"}},  # 全零桶应跳过
                     ]},
                ]}}}
    rows1 = sync._normalize_amount_bykey(raw1)
    assert len(rows1) == 4, rows1
    by_type = {r["type"]: r["amount"] for r in rows1}
    assert by_type == {
        "input_cache_hit_tokens": 18119168,
        "input_cache_miss_tokens": 212592,
        "output_tokens": 187968,
        "request_count": 248,
    }
    # 1786464000 应为 2026-08-12(GMT+8 日边界)
    assert rows1[0]["utc_date"] == "2026-08-12", rows1[0]["utc_date"]
    assert rows1[0]["api_key_name"] == "Zcode2"
    assert rows1[0]["model"] == "deepseek-v4-pro"

    # --- by_api_key/cost: data[].series[].buckets[].cost, 含币种 ---
    raw2 = {"code": "0", "data": {"biz_data": {
        "start": 1786204800, "end": 1786550400, "bucket": 86400,
        "models": ["deepseek-v4-pro"], "data": [{
            "currency": "CNY",
            "series": [{
                "api_key": {"name": "Zcode2"}, "model": "deepseek-v4-pro",
                "buckets": [
                    {"time": 1786464000, "cost": "2.2185632000000000"},
                    {"time": 1786377600, "cost": "0"},  # 全零跳过
                ]}]}]}}}
    rows2 = sync._normalize_cost_bykey(raw2)
    assert len(rows2) == 1, rows2
    assert rows2[0]["utc_date"] == "2026-08-12"
    assert abs(rows2[0]["cost"] - 2.2185632) < 1e-6
    assert rows2[0]["currency"] == "CNY" and rows2[0]["api_key_name"] == "Zcode2"

    # --- 空/异常形态 ---
    assert sync._normalize_amount_bykey({"code": "0", "data": {"biz_data": {"series": []}}}) == []
    assert sync._normalize_cost_bykey({"code": "0", "data": {"biz_data": None}}) == []

    # --- 类型映射 ---
    assert sync._map_type("PROMPT_CACHE_HIT_TOKEN") == "input_cache_hit_tokens"
    assert sync._map_type("REQUEST") == "request_count"
    assert sync._map_type("UNKNOWN_TYPE") == "unknown_type"

    # --- 时间戳换算(日边界对齐) ---
    assert sync._ts_to_date(1786464000) == "2026-08-12"
    assert sync._ts_to_date(1786377600) == "2026-08-11"

    # --- 账号指纹: 同 Key 集合稳定, 不同 Key 集合不同, 无数据为 empty ---
    fp1 = sync._account_fingerprint({"series": [
        {"api_key": {"tracking_id": "aaa", "name": "Key1"}, "model": "m"},
        {"api_key": {"tracking_id": "bbb", "name": "Key2"}, "model": "m"},
    ]})
    fp1b = sync._account_fingerprint({"series": [
        {"api_key": {"tracking_id": "bbb", "name": "Key2"}, "model": "m"},
        {"api_key": {"tracking_id": "aaa", "name": "Key1"}, "model": "m"},
    ]})
    fp2 = sync._account_fingerprint({"series": [
        {"api_key": {"tracking_id": "ccc", "name": "Other"}, "model": "m"},
    ]})
    assert fp1 == fp1b, "同集合顺序无关应一致"
    assert fp1 != fp2, "不同账号应不同"
    assert sync._account_fingerprint({"series": []}) == "empty"
    assert sync._account_fingerprint(None) == "empty"
    assert len(fp1) == 16

    # --- 账号冲突判定: 有交集=同账号(Key 增删不算切换), 完全不相交=疑似切换, 空集放行 ---
    assert not sync._accounts_conflict(frozenset(["a"]), frozenset(["a", "b"])), "新增 Key 不算切换"
    assert not sync._accounts_conflict(frozenset(["a", "b"]), frozenset(["b"])), "减少 Key 不算切换"
    assert sync._accounts_conflict(frozenset(["a"]), frozenset(["c"])) is True, "完全不相交=疑似切换"
    assert not sync._accounts_conflict(frozenset(), frozenset(["a"])), "任一方为空放行"
    assert not sync._accounts_conflict(frozenset(["a"]), frozenset()), "任一方为空放行"

    # --- 分时(bucket=3600): 保留模型/Key 维度, 全零跳过, 冗余类型忽略 ---
    raw_h = {"code": "0", "data": {"biz_code": "0", "biz_msg": "", "biz_data": {
        "start": 1786464000, "end": 1786550400, "bucket": 3600,
        "models": ["deepseek-v4-pro", "deepseek-v4-flash"], "series": [
            {"api_key": {"name": "K1"}, "model": "deepseek-v4-pro", "buckets": [
                {"time": 1786464000, "usage": {"PROMPT_CACHE_HIT_TOKEN": "100", "REQUEST": "2"}},
                {"time": 1786467600, "usage": {"PROMPT_CACHE_MISS_TOKEN": "50",
                                               "RESPONSE_TOKEN": "30"}},
                {"time": 1786464000, "usage": {"REQUEST": "0"}},                    # 全零跳过
            ]},
            {"api_key": {"name": "K2"}, "model": "deepseek-v4-flash", "buckets": [
                {"time": 1786464000, "usage": {"PROMPT_CACHE_HIT_TOKEN": "25"}},    # 不同维度不合并
            ]},
        ]}}}
    rows_h = sync._normalize_amount_hourly(raw_h)
    assert rows_h == [
        {"utc_date": "2026-08-12", "hour": 0, "model": "deepseek-v4-pro", "api_key_name": "K1",
         "type": "input_cache_hit_tokens", "amount": 100},
        {"utc_date": "2026-08-12", "hour": 0, "model": "deepseek-v4-pro", "api_key_name": "K1",
         "type": "request_count", "amount": 2},
        {"utc_date": "2026-08-12", "hour": 1, "model": "deepseek-v4-pro", "api_key_name": "K1",
         "type": "input_cache_miss_tokens", "amount": 50},
        {"utc_date": "2026-08-12", "hour": 1, "model": "deepseek-v4-pro", "api_key_name": "K1",
         "type": "output_tokens", "amount": 30},
        {"utc_date": "2026-08-12", "hour": 0, "model": "deepseek-v4-flash", "api_key_name": "K2",
         "type": "input_cache_hit_tokens", "amount": 25},
    ], rows_h
    # 1786464000 = 2026-08-12 00:00 (GMT+8), 1786467600 = 01:00
    assert sync._ts_to_hour(1786464000) == ("2026-08-12", 0)
    assert sync._ts_to_hour(1786467600) == ("2026-08-12", 1)

    raw_hc = {"code": "0", "data": {"biz_data": {"bucket": 3600, "data": [{
        "currency": "CNY", "series": [{"model": "m", "api_key": {"name": "K1"}, "buckets": [
            {"time": 1786464000, "cost": "1.5"},
            {"time": 1786464000, "cost": "0.25"},   # 同维度同小时, 归一化不合并(主键覆盖)
            {"time": 1786467600, "cost": "0"},      # 全零跳过
        ]}]}]}}}
    rows_hc = sync._normalize_cost_hourly(raw_hc)
    assert rows_hc == [
        {"utc_date": "2026-08-12", "hour": 0, "model": "m", "api_key_name": "K1",
         "cost": 1.5, "currency": "CNY"},
        {"utc_date": "2026-08-12", "hour": 0, "model": "m", "api_key_name": "K1",
         "cost": 0.25, "currency": "CNY"},
    ], rows_hc
    assert sync._normalize_amount_hourly({"code": "0", "data": {"biz_data": {"series": []}}}) == []
    assert sync._normalize_cost_hourly(None) == []

    print("[ok] 归一化层 20 组用例通过(by_api_key 真实格式 + 账号指纹 + 分时 bucket=3600 带维度)")


def test_mock_and_api():
    port = 8791  # 独立端口, 避免与用户运行中的应用(8765)冲突
    server.set_mode("mock")
    db.set_mode("test")   # 测试使用独立临时库
    mock_data.generate()

    t = threading.Thread(target=lambda: __import__("uvicorn").Server(
        __import__("uvicorn").Config(server.app, host="127.0.0.1", port=port,
                                     log_level="critical")).run(), daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            requests.get(base + "/api/auth/status", timeout=1)
            break
        except requests.RequestException:
            time.sleep(0.2)

    # 认证状态
    r = requests.get(base + "/api/auth/status").json()
    assert r["mode"] == "mock" and r["configured"] is True
    print("[ok] auth/status:", r)

    # 汇总
    s = requests.get(base + "/api/stats/summary").json()
    assert s["total"]["tokens"] > 0 and s["month"]["tokens"] > 0
    assert "CNY" in s["total"]["cost"]
    assert s["dates"]["earliest"] and s["dates"]["latest"]
    print(f"[ok] summary: 累计 {s['total']['tokens']} tokens, "
          f"花费 {s['total']['cost']}, 范围 {s['dates']['earliest']} ~ {s['dates']['latest']}")

    # 每日
    d = requests.get(base + "/api/stats/daily?days=90").json()
    assert len(d["rows"]) > 0
    models = {r["model"] for r in d["rows"]}
    print(f"[ok] daily: {len(d['rows'])} 行, 模型 {sorted(models)}")

    # 全部历史(days=0): 从最早数据日期到今天
    d0 = requests.get(base + "/api/stats/daily?days=0").json()
    assert d0["start"] == s["dates"]["earliest"], (d0["start"], s["dates"]["earliest"])
    assert d0["end"] == date.today().isoformat()
    assert len(d0["rows"]) > 0
    print(f"[ok] daily days=0: 范围 {d0['start']} ~ {d0['end']} ({len(d0['rows'])} 行)")

    # 按 API Key 分组
    dk = requests.get(base + "/api/stats/daily?days=90&group=key").json()
    assert dk["group"] == "key" and len(dk["rows"]) > 0
    keys = {r["api_key_name"] for r in dk["rows"]}
    assert keys == {"默认 API Key", "生产环境", "测试环境"}, keys
    print(f"[ok] daily group=key: {len(dk['rows'])} 行, API Key {sorted(keys)}")

    # 模型占比
    m = requests.get(base + "/api/stats/models").json()
    assert len(m) == len(mock_data.MODEL_PROFILE) and all(x["tokens"] > 0 for x in m)
    print(f"[ok] models: {[(x['model'], x['tokens']) for x in m]}")

    # 热力图(近 366 天含首尾, 日期动态计算)
    hs = (date.today() - timedelta(days=365)).isoformat()
    he = date.today().isoformat()
    h = requests.get(base + f"/api/stats/heatmap?start={hs}&end={he}&metric=tokens").json()
    assert len(h["rows"]) == 366
    hc = requests.get(base + f"/api/stats/heatmap?start={hs}&end={he}&metric=cost").json()
    assert len(hc["rows"]) == 366
    print("[ok] heatmap: 366 天 tokens + cost 数据齐全")

    # 累计(mock 生成 730 天)
    c = requests.get(base + "/api/stats/cumulative?metric=tokens").json()
    assert len(c) == 730, len(c)
    last = c[-1]["total"]
    assert last == s["total"]["tokens"], f"累计终点 {last} != 汇总 {s['total']['tokens']}"
    print(f"[ok] cumulative: 终点累计 {last} 与汇总一致")

    # 分时: 范围聚合(24 项) 与 单日明细(今天/昨天有, 更早为空)
    ha = requests.get(base + "/api/stats/hourly/aggregate?days=30").json()
    assert len(ha["hours"]) == 24, len(ha["hours"])
    assert any(h["cache_hit"] + h["cache_miss"] + h["output"] > 0 for h in ha["hours"])
    assert any(h["requests"] > 0 for h in ha["hours"])
    assert any(h["cost"].get("CNY", 0) > 0 for h in ha["hours"])
    # 聚合范围应从今天往前 30 天(含首尾), 分时仅今天/昨天有数据 → 聚合总量 = 这两天之和
    hd_today = requests.get(base + f"/api/stats/hourly/detail?date={date.today().isoformat()}").json()
    hd_yest = requests.get(base + f"/api/stats/hourly/detail?date={(date.today() - timedelta(days=1)).isoformat()}").json()
    assert len(hd_today["hours"]) == 24 and len(hd_yest["hours"]) == 24
    assert any(h["cache_hit"] + h["cache_miss"] + h["output"] > 0 for h in hd_today["hours"])
    assert any(h["requests"] > 0 for h in hd_yest["hours"])
    old = (date.today() - timedelta(days=3)).isoformat()
    hd_old = requests.get(base + f"/api/stats/hourly/detail?date={old}").json()
    assert all(h["cache_hit"] + h["cache_miss"] + h["output"] + h["requests"] == 0
               for h in hd_old["hours"]), "更早日期不应有分时数据"
    sum_t = sum(h["cache_hit"] + h["cache_miss"] + h["output"] for h in hd_today["hours"])
    sum_y = sum(h["cache_hit"] + h["cache_miss"] + h["output"] for h in hd_yest["hours"])
    agg_t = sum(h["cache_hit"] + h["cache_miss"] + h["output"] for h in ha["hours"])
    assert abs(agg_t - (sum_t + sum_y)) <= max(sum_t, sum_y) * 0.02, "分时聚合应≈今天+昨天之和(分摊取整误差)"
    print(f"[ok] hourly: 聚合 30 天 24 项; 今天 {sum_t} / 昨天 {sum_y} tokens; 更早日期为空")

    # 分时明细: 按模型/API Key 分组视图(各分组小时之和 = 计费类型视图总量)
    hm = requests.get(base + f"/api/stats/hourly/detail?date={date.today().isoformat()}&group=model").json()
    assert hm["group"] == "model" and len(hm["hours"]) == 24
    assert set(hm["groups"]) == set(mock_data.MODEL_PROFILE), hm["groups"]
    sum_m = sum(sum(h["values"].values()) for h in hm["hours"])
    assert abs(sum_m - sum_t) <= max(sum_t, 1) * 0.02, f"按模型 {sum_m} != 按类型 {sum_t}"
    assert sum(h["requests"] for h in hm["hours"]) == sum(h["requests"] for h in hd_today["hours"])
    # 分组请求数: 每组请求之和 = 该小时总请求数
    assert all(sum(h["reqValues"].values()) == h["requests"] for h in hm["hours"])
    assert all(any(v > 0 for v in h["reqValues"].values()) for h in hm["hours"])
    # 分组费用: 各组各币种之和 = 该小时总费用
    assert all(
        abs(sum(v for per in h["costValues"].values() for v in per.values())
            - sum(h["cost"].values())) < 1e-4
        for h in hm["hours"]
    )
    hk = requests.get(base + f"/api/stats/hourly/detail?date={date.today().isoformat()}&group=key").json()
    assert hk["group"] == "key" and len(hk["hours"]) == 24 and len(hk["groups"]) >= 1
    assert all(h["values"] for h in hk["hours"])
    sum_k = sum(sum(h["values"].values()) for h in hk["hours"])
    assert abs(sum_k - sum_t) <= max(sum_t, 1) * 0.02
    assert all(sum(h["reqValues"].values()) == h["requests"] for h in hk["hours"])
    print(f"[ok] hourly group: 按模型 {hm['groups']} / 按 Key {hk['groups']} 分组与总量一致")

    # 同步( mock 模式重新生成数据)
    r = requests.post(base + "/api/sync").json()
    assert r["ok"]
    logs = requests.get(base + "/api/sync/logs").json()
    assert len(logs) >= 1
    print("[ok] sync + logs:", logs[0]["message"])

    # 未登录状态下的错误处理(live 模式无 token 时同步应返回 400)
    # 注: 若本机已配置真实 token(real_sync 写入), 临时移走再断言
    import os as _os
    from app.config import TOKEN_PATH as _TP
    had_token = _os.path.exists(_TP)
    if had_token:
        _os.rename(_TP, str(_TP) + ".testbak")
    try:
        server.set_mode("live")
        r = requests.post(base + "/api/sync")
        assert r.status_code == 400
        print("[ok] live 模式未登录时同步返回 400:", r.json()["detail"])
    finally:
        server.set_mode("mock")
        db.set_mode("test")   # 测试使用独立临时库
        if had_token:
            _os.rename(str(_TP) + ".testbak", _TP)

    print("\n全部测试通过 ✅")


def _start_server(port: int, mode: str) -> str:
    """启动测试服务, 返回 base url。"""
    server.set_mode(mode)
    db.set_mode("test")   # 测试统一使用独立临时库
    t = threading.Thread(target=lambda: __import__("uvicorn").Server(
        __import__("uvicorn").Config(server.app, host="127.0.0.1", port=port,
                                     log_level="critical")).run(), daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            requests.get(base + "/api/auth/status", timeout=1)
            break
        except requests.RequestException:
            time.sleep(0.2)
    return base


def test_logout():
    """退出登录: 清除凭证与余额缓存, 且不影响本地历史数据。"""
    import os as _os
    from app.config import TOKEN_PATH as _TP

    had_token = _os.path.exists(_TP)
    saved = None
    if had_token:
        with open(_TP, encoding="utf-8") as f:
            saved = f.read()
    # 确保有凭证可登出(用假凭证文件模拟已配置状态)
    if not had_token:
        sync.save_token("fake-token-for-logout-test-000000")

    base = _start_server(8766, "live")
    try:
        st0 = requests.get(base + "/api/auth/status").json()
        assert st0["configured"] is True, st0
        r = requests.post(base + "/api/logout", timeout=10)
        assert r.status_code == 200, r.text
        st = requests.get(base + "/api/auth/status").json()
        assert st["configured"] is False, st
        print("[ok] logout: 凭证已清除, 状态变为未配置")
    finally:
        if saved:
            with open(_TP, "w", encoding="utf-8") as f:
                f.write(saved)
        elif not had_token:
            _os.path.exists(_TP) and _os.remove(_TP)


def test_sync_trigger_live():
    """live 模式 + 已配置凭证时触发同步: 应返回 200 而非 500。

    曾因 trigger_sync 中 _sync_state 赋值导致 UnboundLocalError(回归用例)。
    """
    saved = sync.load_token()
    sync.save_token("fake-token-for-sync-trigger-test")  # 假凭证足够触发流程
    base = _start_server(8767, "live")
    try:
        r = requests.post(base + "/api/sync", timeout=10)
        assert r.status_code == 200, r.text
        # 后台线程因假凭证异步失败 → status 显示 error, 而非触发端崩溃
        st = None
        for _ in range(30):
            st = requests.get(base + "/api/sync/status").json()
            if not st["running"]:
                break
            time.sleep(0.5)
        assert st and st["error"] is not None, st
        assert "登录" in st["error"]["message"], st
        print("[ok] sync 触发(live+已配置): 200, 后台失败正常上报:", st["error"]["message"])
    finally:
        if saved:
            sync.save_token(saved)
        else:
            sync.clear_token()


def test_clear_data():
    """清空全部数据: 表清空、凭证保留。"""
    from app import mock_data
    from app.config import TOKEN_PATH as _TP
    import os as _os

    had_token = _os.path.exists(_TP)
    saved = None
    if had_token:
        with open(_TP, encoding="utf-8") as f:
            saved = f.read()

    base = _start_server(8768, "mock")
    mock_data.generate()
    try:
        s0 = requests.get(base + "/api/stats/summary").json()
        assert s0["total"]["tokens"] > 0
        r = requests.post(base + "/api/data/clear", timeout=10)
        assert r.status_code == 200, r.text
        s1 = requests.get(base + "/api/stats/summary").json()
        assert s1["total"]["tokens"] == 0
        assert s1["dates"]["earliest"] is None
        logs = requests.get(base + "/api/sync/logs").json()
        assert logs == []
        h1 = requests.get(base + "/api/stats/hourly/aggregate?days=7").json()
        assert all(h["cache_hit"] + h["cache_miss"] + h["output"] + h["requests"] == 0
                   for h in h1["hours"]), "清空后分时数据应为空"
        # 凭证保留(测试已确保存在凭证, 清空后应仍为已配置)
        st = requests.get(base + "/api/auth/status").json()
        assert st["configured"] is True, st
        print("[ok] clear: 数据已清空, 凭证保留:", r.json()["message"])
    finally:
        if saved:
            with open(_TP, "w", encoding="utf-8") as f:
                f.write(saved)


def test_export_import():
    """导出 → 清空 → 导入: 数据完全恢复; 非法文件被拒绝且不动数据。"""
    base = _start_server(8769, "mock")
    mock_data.generate()
    try:
        # 导出
        r = requests.get(base + "/api/data/export")
        assert r.status_code == 200
        backup = r.json()
        assert backup["app"] == "deepseek-usage-stats" and backup["version"] == 1
        assert backup["amount_daily"] and backup["cost_daily"]
        assert backup["hourly_usage"] and backup["hourly_cost"]
        s0 = requests.get(base + "/api/stats/summary").json()
        h0 = requests.get(base + "/api/stats/hourly/aggregate?days=7").json()
        h0_total = sum(h["cache_hit"] + h["cache_miss"] + h["output"] + h["requests"]
                       for h in h0["hours"])

        # 非法文件: 400 且数据不动
        bad = requests.post(base + "/api/data/import",
                            json={"app": "other", "version": 1})
        assert bad.status_code == 400, bad.text
        bad2 = requests.post(base + "/api/data/import",
                             json={"app": "deepseek-usage-stats", "version": 1,
                                   "amount_daily": [{"utc_date": "x"}], "cost_daily": [],
                                   "hourly_usage": [], "hourly_cost": []})
        assert bad2.status_code == 400, bad2.text
        s_after_bad = requests.get(base + "/api/stats/summary").json()
        assert s_after_bad["total"]["tokens"] == s0["total"]["tokens"], "非法导入不应改动数据"

        # 清空后导入恢复
        requests.post(base + "/api/data/clear")
        assert requests.get(base + "/api/stats/summary").json()["total"]["tokens"] == 0
        imp = requests.post(base + "/api/data/import", json=backup)
        assert imp.status_code == 200, imp.text
        s2 = requests.get(base + "/api/stats/summary").json()
        assert s2["total"]["tokens"] == s0["total"]["tokens"], "导入后总量应恢复"
        assert s2["dates"]["earliest"] == s0["dates"]["earliest"]
        assert s2["dates"]["latest"] == s0["dates"]["latest"]
        h2 = requests.get(base + "/api/stats/hourly/aggregate?days=7").json()
        h2_total = sum(h["cache_hit"] + h["cache_miss"] + h["output"] + h["requests"]
                       for h in h2["hours"])
        assert h2_total == h0_total, "分时数据应随导入恢复"
        print(f"[ok] export/import: 用量/费用/分时往返一致"
              f"({imp.json()['message']})")
    finally:
        server.set_mode("mock")
        db.set_mode("test")


def test_auto_backup():
    """同步前自动备份: 生成完整备份、最多保留 BACKUP_KEEP 份、mock 模式不备份、live 触发时备份。"""
    import json as _json
    import shutil
    import tempfile

    import app.config as config
    from app.config import BACKUP_KEEP

    tmp = Path(tempfile.mkdtemp(prefix="zcode-backup-test-"))
    old_dir = config.BACKUP_DIR
    config.BACKUP_DIR = tmp   # 重定向到临时目录(_save_auto_backup 动态读取)
    try:
        server.set_mode("mock")
        db.set_mode("test")
        mock_data.generate()

        # 1. 生成备份且结构完整
        name = server._save_auto_backup()
        assert (tmp / name).exists()
        payload = _json.loads((tmp / name).read_text(encoding="utf-8"))
        assert payload["app"] == "deepseek-usage-stats" and payload["version"] == 1
        assert payload["amount_daily"] and payload["hourly_usage"]

        # 2. 保留策略: 连续生成 25 份后只剩 BACKUP_KEEP 份(最旧的被删)
        for _ in range(25):
            server._save_auto_backup()
        files = sorted(tmp.glob("backup-*.json"))
        assert len(files) == BACKUP_KEEP, f"应保留 {BACKUP_KEEP} 份, 实际 {len(files)}"

        # 3. mock 模式触发同步: 不产生自动备份
        before = len(list(tmp.glob("backup-*.json")))
        res = server.trigger_sync()
        assert res["ok"] and "自动备份" not in res["message"]
        assert len(list(tmp.glob("backup-*.json"))) == before

        # 4. live + 已配置凭证触发同步: 先自动备份再启动(线程替换为 no-op 防写真实库)
        orig_run = server._run_sync_async
        server._run_sync_async = lambda: None
        sync.save_token("fake-token-for-backup-test-000000")
        try:
            server.set_mode("live")
            db.set_mode("test")   # 备份打包测试库, 不触碰真实库
            before_files = {p.name for p in tmp.glob("backup-*.json")}
            res = server.trigger_sync()
            assert "已自动备份" in res["message"], res
            after_files = {p.name for p in tmp.glob("backup-*.json")}
            assert after_files - before_files, "同步触发应生成新备份"
            assert len(after_files) == BACKUP_KEEP, "备份数不应超过上限"
        finally:
            server._run_sync_async = orig_run
            server.set_mode("mock")
            db.set_mode("test")
            sync.clear_token()
        print(f"[ok] auto_backup: 生成/保留 {BACKUP_KEEP} 份/mock 不备份/live 触发备份")
    finally:
        config.BACKUP_DIR = old_dir
        shutil.rmtree(tmp, ignore_errors=True)


def test_backup_keep_daily():
    """保留策略: 最近 BACKUP_KEEP 份 + 最近 KEEP_DAILY_DAYS 天每天最新一份。"""
    import shutil
    import tempfile

    import app.config as config
    from app.config import BACKUP_KEEP, KEEP_DAILY_DAYS

    def touch(tmp: Path, day: date, hour: str) -> None:
        (tmp / f"backup-{day.strftime('%Y%m%d')}-{hour}-000000.json").write_text("{}", encoding="utf-8")

    tmp = Path(tempfile.mkdtemp(prefix="zcode-backup-daily-"))
    old_dir = config.BACKUP_DIR
    config.BACKUP_DIR = tmp
    try:
        today = date.today()
        # 过去 30 天每天两份备份(早上/晚上)
        for i in range(30):
            d = today - timedelta(days=i)
            touch(tmp, d, "080000")
            touch(tmp, d, "200000")

        server._prune_backups()
        remaining = {p.name for p in tmp.glob("backup-*.json")}

        # 最近 20 份 = 最近 10 天 × 每天 2 份, 全部保留
        for i in range(10):
            d = today - timedelta(days=i)
            day = d.strftime("%Y%m%d")
            assert f"backup-{day}-080000-000000.json" in remaining
            assert f"backup-{day}-200000-000000.json" in remaining

        # 最近 KEEP_DAILY_DAYS 天每天最新一份(晚上)保留 —— 覆盖超出最近 20 份的日期
        for i in range(KEEP_DAILY_DAYS):
            d = today - timedelta(days=i)
            day = d.strftime("%Y%m%d")
            assert f"backup-{day}-200000-000000.json" in remaining, f"今天-{i} 天应保留最新备份"

        # 超出最近 20 份且超出 14 天保护范围的删除(如今天-10 的早上份、今天-14 全部)
        d10 = (today - timedelta(days=10)).strftime("%Y%m%d")
        assert f"backup-{d10}-080000-000000.json" not in remaining, "超限且非当天最新应删除"
        d14 = (today - timedelta(days=KEEP_DAILY_DAYS)).strftime("%Y%m%d")
        assert f"backup-{d14}-200000-000000.json" not in remaining, f"{KEEP_DAILY_DAYS} 天前不应保留"
        d20 = (today - timedelta(days=20)).strftime("%Y%m%d")
        assert f"backup-{d20}-080000-000000.json" not in remaining

        # 预期数量: 最近 20 份(覆盖今天-9~今天) + 14 天保护中额外覆盖今天-10~今天-13 的晚上份(4 份) = 24
        assert len(remaining) == BACKUP_KEEP + (KEEP_DAILY_DAYS - 10), len(remaining)
        print(f"[ok] backup_keep_daily: 最近 {BACKUP_KEEP} 份 + 最近 {KEEP_DAILY_DAYS} 天每天最新一份")
    finally:
        config.BACKUP_DIR = old_dir
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_normalize()
    test_mock_and_api()
    test_logout()
    test_sync_trigger_live()
    test_clear_data()
    test_export_import()
    test_auto_backup()
    test_backup_keep_daily()
