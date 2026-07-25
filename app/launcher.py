"""启动本地服务，并在默认浏览器中打开管理页面。"""

import threading
import webbrowser

import uvicorn

HOST = "127.0.0.1"
PORT = 8765
APP_URL = f"http://{HOST}:{PORT}"


def open_browser() -> None:
    """稍候打开页面，为本地服务预留启动时间。"""
    webbrowser.open(APP_URL)


def main() -> None:
    timer = threading.Timer(2.0, open_browser)
    timer.daemon = True
    timer.start()
    uvicorn.run("app.main:app", host=HOST, port=PORT)


if __name__ == "__main__":
    main()
