# 开发版交接
## 现状
目录 `D:\tools\steam-market-auto-lister-dev`，分支 `develop`；稳定版在 `D:\tools\steam-market-auto-lister`。版本以 `git log -1` 为准。数据库使用 `(appid,contextid,assetid)` 复合键。
## 最近提交
本次维护新增持久化上架请求元数据，修复完整运行重建后阶段退回 trend；新上架任务支持自定义价格；新任务和价格异常按同情况折叠；库存数量只显示数字。完整运行遇到价格异常会启动本地网页并定位异常区，启动清理会保留 `price_review`；自定义价格操作旁可打开对应 Steam 市场页面。此前提交：`10c0441` 修复启动挂单冲突；`2144602` 清理待手机确认；`5afd64c` 修复跨上下文覆盖。
## 已完成
库存、挂单、待确认匹配使用复合键；旧 SQLite 自动迁移；启动/完整运行清理 `pending_confirmation`、保留 `active`，并从 `listing_submissions` 与调价历史恢复请求时间、阶段、策略和下次动作；调价时间基准、阶段策略、黑名单、异常提示已实现；Listing ID 冲突不再崩溃。56 项测试、Ruff 与 JavaScript 语法检查通过。
## 待办/已知问题
Steam 是非公开网页接口，页面变化可能破坏解析。无 Listing ID 时同名饰品只能名称兜底，有误匹配风险。旧主键覆盖的库存需重新同步。稳定版/GitHub 勿主动操作。
## 关键决策
assetid 不是全局唯一，必须带 AppID/contextid。下一动作优先 Steam 上架日期，同日期才用持久化的本地请求时分秒。真实写入须同时开启两个开关；手机确认由用户完成。`active` 跨重启保留，`pending_confirmation` 不缓存，但提交元数据永久保留供重建。
## 配置项
前缀 `STEAM_LISTER_`；关键项：`DRY_RUN`、`ALLOW_MARKET_WRITES`、`DEFAULT_CURRENCY`、`SYNC_INTERVAL_SECONDS`、请求延迟/超时/重试、库存分页、批次上限和单件价格上限。默认演练、禁止写入，详见 `app/core/config.py`。
## 运行/测试
```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
.\run-with-browser.bat
.\run-full-once.bat
python -m pytest -q
python -m ruff check app tests
```
先登录 Steam；真实操作先低价验证。
