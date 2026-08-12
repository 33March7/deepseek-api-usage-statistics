"""验证 /api/token 的验证逻辑: 无效凭证 400 且不影响已保存凭证(有状态保护)。"""
import sys
import threading
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import server, sync  # noqa: E402

server.set_mode("live")
import uvicorn  # noqa: E402

t = threading.Thread(target=lambda: uvicorn.run(
    server.app, host="127.0.0.1", port=8799, log_level="critical"), daemon=True)
t.start()
time.sleep(2)
base = "http://127.0.0.1:8799"

saved_token = sync.load_token()
assert saved_token, "需要已配置的 token"
try:
    # 1. 无效 token → 应 400, 且已保存凭证不被覆盖
    r1 = requests.post(base + "/api/token", json={"token": "invalid-token-abc"}, timeout=30)
    assert r1.status_code == 400, r1.text
    assert sync.load_token() == saved_token, "无效 token 不应覆盖已保存凭证"
    print("[ok] 无效 token → 400 且原凭证保留")

    # 2. 有效 token → 200
    r2 = requests.post(base + "/api/token", json={"token": saved_token}, timeout=30)
    assert r2.status_code == 200 and r2.json()["valid"], r2.text
    print("[ok] 有效 token → 200:", r2.json())

    # 3. 格式过短 → 400
    r3 = requests.post(base + "/api/token", json={"token": "short"}, timeout=30)
    assert r3.status_code == 400
    print("[ok] 过短 token → 400:", r3.json()["detail"])
    print("\ntoken 验证接口测试通过 ✅")
finally:
    # 状态保护: 恢复原凭证
    sync.save_token(saved_token)
