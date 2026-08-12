"""开发辅助: 以 mock 模式启动本地服务(供浏览器预览界面)。

用法: py dev_server.py [端口]
"""
from __future__ import annotations

import sys

from app import mock_data, server

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
server.set_mode("mock")
mock_data.generate()

import uvicorn
uvicorn.run(server.app, host="127.0.0.1", port=port, log_level="info")
