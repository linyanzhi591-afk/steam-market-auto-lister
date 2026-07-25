import asyncio

from app.core.database import database
from app.core.models import SessionState
from app.services.listing_manager import listing_manager
from app.services.steam_session import steam_session_service


async def run_once() -> int:
    database.initialize()
    database.clear_runtime_cache()
    session = await steam_session_service.restore()
    if session.state is not SessionState.LOGGED_IN:
        print(f"[错误] Steam 登录未恢复：{session.message}")
        print("请先使用带浏览器的启动脚本登录 Steam。")
        return 1
    if session.wallet_currency:
        database.save_currency(session.wallet_currency)

    print("[1/4] 同步库存与 Steam 当前挂单")
    print("[2/4] 检查全部非黑名单超时挂单")
    print("[3/4] 为全部非黑名单可出售库存生成计划")
    print("[4/4] 执行全部正常待提交计划")
    result = await listing_manager.full_run()
    print(
        "运行结果："
        f"库存 {result.inventory_count}，可出售 {result.marketable_count}，"
        f"超时处理 {result.expired_processed}，计划 {result.plans_created}，"
        f"已提交 {result.listings_submitted}，异常价格 {result.price_reviews}"
    )
    if result.errors:
        print(f"\n发现 {len(result.errors)} 个问题：")
        for index, error in enumerate(result.errors, 1):
            print(f"{index}. {error}")
        return 1
    print("完整运行成功，没有发现错误。")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(run_once()))


if __name__ == "__main__":
    main()
