"""桌面壳: 启动本地服务 + pywebview 主窗口。

- 主窗口: 内嵌渲染本地仪表盘 (http://127.0.0.1:<port>/)
- 登录窗口: 打开 platform.deepseek.com, 用户手动登录后自动提取 userToken
  (localStorage["userToken"] 的 JSON 包装 {"value": ...}), 后端验证通过才关窗
- 兜底: WebView2 不可用时回退到系统默认浏览器
"""
from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
import webbrowser

import requests

from . import server, sync
from .config import PLATFORM_BASE

# 登录窗口提取 token 的 JS(在 platform.deepseek.com 页面上下文执行)
#
# 注意(2026-08 实测): 登录后 userToken 存于 localStorage["userToken"],
# 值为 JSON 包装 {"value": "...", "__version": "0"}, token 本体是不透明串
# (非 JWT)。登录页 localStorage 里有 __tea_cache_tokens_* 等名字含
# "token" 的无关缓存 —— 绝不能按"键名含 token"扫描, 否则会误抓导致
# 窗口秒关(此前 bug 的根因)。
EXTRACT_TOKEN_JS = r"""
(function(){
  function unwrap(v) {
    if (!v) return null;
    v = v.trim();
    if (v.charAt(0) === '{') {
      try {
        var o = JSON.parse(v);
        // 平台 userToken 的包装形如 {"value":"...","__version":"0"};
        // value 为空(未登录时为 null)一律视为未登录
        if (o && typeof o.value === 'string' && o.value.trim()) {
          v = o.value.trim();
        } else {
          return null;
        }
      } catch (e) { return null; }
    }
    if (!v || v.length < 10) return null;
    return v;
  }
  try {
    // 登录/验证类页面不提取(登录完成后平台会跳转到 dashboard)
    var path = location.pathname || '';
    if (/sign|login|auth|verify|captcha|error/i.test(path)) return null;
    var storages = [window.localStorage, window.sessionStorage];
    for (var si = 0; si < storages.length; si++) {
      var s = storages[si];
      var v = null;
      try { v = s.getItem('userToken'); } catch (e) {}
      if (v) { var t = unwrap(v); if (t) return t; }
      // 兜底白名单(仅这些键, 不扫描任意含 token 的键)
      var keys = ['user_token', 'platformToken', 'platform_token', 'access_token', 'auth_token'];
      for (var i = 0; i < keys.length; i++) {
        try { v = s.getItem(keys[i]); } catch (e) { continue; }
        if (v) { var t2 = unwrap(v); if (t2 && t2.length > 20) return t2; }
      }
    }
  } catch (e) {}
  return null;
})()
"""


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_server(port: int, mock: bool) -> None:
    """在后台线程运行 FastAPI 服务。"""
    import uvicorn

    server.set_mode("mock" if mock else "live")
    config = uvicorn.Config(server.app, host="127.0.0.1", port=port,
                            log_level="warning")
    uvicorn.Server(config).run()


def make_server_ready(base_url: str, timeout: float = 10.0) -> bool:
    """等待服务就绪。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            requests.get(base_url + "/api/auth/status", timeout=2)
            return True
        except requests.RequestException:
            time.sleep(0.2)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="DeepSeek API 用量统计")
    parser.add_argument("--mock", action="store_true",
                        help="mock 模式: 使用演示数据预览界面, 无需登录")
    args = parser.parse_args()

    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    # 1. 启动后端服务(后台线程)
    server_thread = threading.Thread(target=run_server, args=(port, args.mock),
                                     daemon=True)
    server_thread.start()
    if not make_server_ready(base_url):
        print("本地服务启动失败, 请检查端口占用或依赖安装")
        return 1

    # 2. 登录窗口控制: 由后端 API 触发(前端点「登录」按钮)
    def open_login_window():
        import webview

        print("[登录] 打开平台登录窗口...")

        def set_title(window, text):
            try:
                window.set_title(text)
            except Exception:
                pass

        def on_loaded(window):
            # 应用处于登出状态(无本地凭证)时: 首次加载先清除平台残留会话
            # (localStorage 里的旧 userToken + 会话 cookies), 刷新后从登录页重新开始,
            # 保证「退出登录 → 重新登录」能完整走一遍登录流程。
            cleared = [False]

            def poll_token():
                if not cleared[0] and not sync.has_token():
                    cleared[0] = True
                    try:
                        window.evaluate_js(
                            "try{localStorage.clear();sessionStorage.clear();}catch(e){}")
                    except Exception:
                        pass
                    try:
                        window.clear_cookies()  # 清 WebView2 会话 cookie(含 httpOnly)
                    except Exception:
                        pass
                    try:
                        window.reload()
                    except Exception:
                        pass
                    threading.Timer(1.5, poll_token).start()
                    return

                try:
                    candidate = window.evaluate_js(EXTRACT_TOKEN_JS)
                except Exception:
                    candidate = None

                if isinstance(candidate, str) and candidate.strip():
                    set_title(window, "已获取凭证, 正在验证…")
                    try:
                        resp = requests.post(f"{base_url}/api/token",
                                             json={"token": candidate.strip()},
                                             timeout=15)
                        if resp.status_code == 200:
                            print("[登录] 凭证验证通过, 关闭窗口")
                            set_title(window, "验证成功, 窗口即将关闭")
                            try:
                                window.destroy()
                            except Exception:
                                pass
                            return
                        # 验证未通过: 说明登录尚未完成/取到了错误值, 继续等待
                        print(f"[登录] 验证未通过(HTTP {resp.status_code}), 继续等待")
                        set_title(window, "尚未登录成功, 请在窗口中完成登录…")
                    except requests.RequestException as e:
                        print(f"[登录] 本地服务通信失败: {e}, 继续等待")
                    threading.Timer(2.0, poll_token).start()
                    return
                threading.Timer(1.5, poll_token).start()
            poll_token()

        try:
            login_window = webview.create_window(
                "DeepSeek 登录 - 请在窗口中完成登录, 成功后自动关闭",
                PLATFORM_BASE, width=1024, height=760, on_top=True)
            login_window.events.loaded += on_loaded
        except Exception as e:
            print(f"[登录] 打开登录窗口失败: {e}")

    server.set_login_callback(lambda: threading.Thread(
        target=open_login_window, daemon=True).start())

    # 3. 主窗口(桌面渲染) — WebView2 不可用时回退浏览器
    try:
        import webview

        main_window = webview.create_window(
            "DeepSeek 用量统计", base_url,
            width=1360, height=860, min_size=(1080, 700))
        webview.start()
        return 0
    except Exception as e:  # WebView2 缺失/初始化失败等
        print(f"桌面窗口启动失败({e}), 将在默认浏览器中打开面板")
        webbrowser.open(base_url)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    sys.exit(main())
