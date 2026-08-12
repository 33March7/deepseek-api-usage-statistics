"""重新生成演示数据 + 验证测试不碰演示库。"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import generate_demo  # noqa: E402

# 1. 重新生成演示数据
r = generate_demo.generate()
print("演示库已重新生成:", r["rows_amount"], "行, tokens:", r["totals"]["tokens"],
      "cost:", r["totals"]["cost"])

# 2. 记录当前演示库状态(测试后对比)
conn = sqlite3.connect("data/mock.db")
before = conn.execute("SELECT COUNT(*) FROM amount_daily").fetchone()[0]
conn.close()
print("测试前演示库行数:", before)
open("data/_demo_before.txt", "w").write(str(before))
