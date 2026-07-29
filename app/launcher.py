"""启动本地服务，并在默认浏览器中打开管理页面。"""

import argparse
import threading
import time
import webbrowser

import uvicorn

HOST = "127.0.0.1"
PORT = 8765
APP_URL = f"http://{HOST}:{PORT}"
PRICE_REVIEW_URL = f"{APP_URL}/#price-review"


def open_browser_when_ready(server: uvicorn.Server, url: str = APP_URL) -> None:
    """等待 Uvicorn 完成启动并输出监听地址后再打开页面。"""
    while not server.started and not server.should_exit:
        time.sleep(0.05)
    if not server.should_exit:
        open_browser(url)


def open_browser(url: str = APP_URL) -> None:
    webbrowser.open(url)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--price-review",
        action="store_true",
        help="Open the price-review section after the local server starts.",
    )
    args = parser.parse_args(argv)
    open_url = PRICE_REVIEW_URL if args.price_review else APP_URL
    config = uvicorn.Config("app.main:app", host=HOST, port=PORT)
    server = uvicorn.Server(config)
    browser_thread = threading.Thread(
        target=open_browser_when_ready,
        args=(server, open_url),
        daemon=True,
    )
    browser_thread.start()
    server.run()


if __name__ == "__main__":
    main()
