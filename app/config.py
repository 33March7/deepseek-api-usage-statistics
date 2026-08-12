"""全局路径与常量配置。"""
from __future__ import annotations

import os
from pathlib import Path

# 项目根目录(本文件位于 app/ 下)
ROOT_DIR = Path(__file__).resolve().parent.parent

# 运行时数据目录(数据库、token、原始响应存档)
DATA_DIR = ROOT_DIR / "data"
DB_PATH = DATA_DIR / "deepseek_usage.db"
DB_PATH_MOCK = DATA_DIR / "mock.db"   # 演示(mock)模式独立数据库, 与实时数据隔离
DB_PATH_TEST = DATA_DIR / "test.db"   # 测试专用临时库, 测试运行不会触碰演示/实时数据
TOKEN_PATH = DATA_DIR / "token.json"
RAW_DIR = DATA_DIR / "raw"
BACKUP_DIR = DATA_DIR / "backup"   # 同步前自动备份目录
BACKUP_KEEP = 20                   # 自动备份保留份数上限(超出删最旧)
KEEP_DAILY_DAYS = 14               # 额外保留: 最近 N 天每天最新的备份各一份

# 前端静态资源目录
WEB_DIR = ROOT_DIR / "web"

# DeepSeek 官方域名
PLATFORM_BASE = "https://platform.deepseek.com"
API_BASE = "https://api.deepseek.com"

# 私有 Dashboard 接口(平台页面同源,需 userToken 认证)
USAGE_AMOUNT_URL = PLATFORM_BASE + "/api/v0/usage/amount"
USAGE_COST_URL = PLATFORM_BASE + "/api/v0/usage/cost"
USER_SUMMARY_URL = PLATFORM_BASE + "/api/v0/users/get_user_summary"
# 按时间范围+时区分桶的接口(平台页面「用量信息」实际使用的接口, 2026-08 实测)
USAGE_BY_KEY_AMOUNT_URL = PLATFORM_BASE + "/api/v0/usage/by_api_key/amount"
USAGE_BY_KEY_COST_URL = PLATFORM_BASE + "/api/v0/usage/by_api_key/cost"

# 统计时区(平台页面默认 GMT+8, 数据按该时区分日桶; 修改后需重新同步)
from datetime import timedelta, timezone as _tz
SYNC_TZ = _tz(timedelta(hours=8))
SYNC_TZ_OFFSET_SEC = int(SYNC_TZ.utcoffset(None).total_seconds())  # 28800

# by_api_key 接口单次请求的范围上限(天, 实测 31 天会被拒绝)
BY_KEY_MAX_RANGE_DAYS = 30

# 逐月回溯的安全上限(月)。超过则停止,防止异常接口返回永远非空
MAX_BACKFILL_MONTHS = 60
# 每次请求超时(秒)
HTTP_TIMEOUT = 30
# 请求间隔(秒),避免对平台造成压力
REQUEST_DELAY = 0.3

# 计费类型 → 中文名
TYPE_LABELS = {
    "request_count": "请求次数",
    "input_cache_hit_tokens": "输入-缓存命中",
    "input_cache_miss_tokens": "输入-缓存未命中",
    "output_tokens": "输出",
    "prompt_tokens": "输入",
    "completion_tokens": "输出",
    "total_tokens": "总计",
}

# 已知 token 计费类型集合(归一化时用于识别"
TOKEN_TYPES = {
    "input_cache_hit_tokens",
    "input_cache_miss_tokens",
    "output_tokens",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
}

# token 过期错误码(平台私有接口)
SESSION_EXPIRED_CODES = {"40002", "40003"}


def ensure_dirs() -> None:
    """确保运行时目录存在。"""
    for d in (DATA_DIR, RAW_DIR, BACKUP_DIR):
        os.makedirs(d, exist_ok=True)
