"""真实全量同步: 用已配置的 token 跑完整同步引擎并校验结果。

用法: py tests/real_sync.py
token 从 data/token.json 读取(由登录/同步流程写入), 不在脚本中硬编码。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db, sync  # noqa: E402


def main():
    token = sync.load_token()
    if not token:
        print("请先配置登录(data/token.json 为空)")
        return 1

    result = sync.run_sync()
    print("\n=== 同步结果 ===")
    for k, v in result.items():
        print(f"  {k}: {v}")

    print("\n=== 数据库校验 ===")
    print("最早日期:", db.earliest_date(), "| 最晚日期:", db.latest_date())
    amounts = db.get_amount_totals()
    print("用量汇总:", {k: v for k, v in amounts.items() if k != "request_count"})
    print("请求数:", amounts.get("request_count", 0))
    print("费用汇总:", db.get_cost_totals())
    print("模型明细:")
    for m in db.get_model_totals():
        print(f"  {m['model']}: tokens={m['tokens']:,} 费用={m['cost']}")

    print("\n=== 余额快照 ===")
    raw = db.get_meta("user_summary")
    if isinstance(raw, str) and raw:
        try:
            raw = json.loads(raw)
        except ValueError:
            pass
    if isinstance(raw, dict):
        print(json.dumps(raw, ensure_ascii=False, indent=1)[:600])

    print("\n=== 最近同步日志 ===")
    for l in db.get_sync_logs(3):
        print(f"  [{l['status']}] {l['started_at']} {l['message']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
