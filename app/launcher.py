"""启动本地服务，并在默认浏览器中打开管理页面。"""

import threading
import time
import webbrowser

import uvicorn

HOST = "127.0.0.1"
PORT = 8765
APP_URL = f"http://{HOST}:{PORT}"


def open_browser_when_ready(server: uvicorn.Server) -> None:
    """等待 Uvicorn 完成启动并输出监听地址后再打开页面。"""
    while not server.started and not server.should_exit:
        time.sleep(0.05)
    if not server.should_exit:
        open_browser()


def open_browser() -> None:
    webbrowser.open(APP_URL)


def main() -> None:
    config = uvicorn.Config("app.main:app", host=HOST, port=PORT)
    server = uvicorn.Server(config)
    browser_thread = threading.Thread(
        target=open_browser_when_ready,
        args=(server,),
        daemon=True,
    )
    browser_thread.start()
    server.run()


if __name__ == "__main__":
    main()
