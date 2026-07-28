# 开发版交接
## 现状
目录 `D:\tools\steam-market-auto-lister-dev`，分支 `develop`；稳定版在 `D:\tools\steam-market-auto-lister`。版本 `10c0441`。数据库已迁移为 `(appid,contextid,assetid)` 复合键。
## 最近提交
`10c0441` 修复启动挂单冲突；`2144602` 清理待手机确认；`5afd64c` 修复跨上下文覆盖；`3db945c` 撤回错误去重。
## 已完成
库存、挂单、待确认匹配使用复合键；旧 SQLite 自动迁移；启动/完整运行清理 `pending_confirmation`、保留 `active`；调价时间基准、阶段策略、黑名单、异常提示已实现；Listing ID 冲突不再崩溃。52 项测试、Ruff 通过。
## 待办/已知问题
Steam 是非公开网页接口，页面变化可能破坏解析。无 Listing ID 时同名饰品只能名称兜底，有误匹配风险。旧主键覆盖的库存需重新同步。稳定版/GitHub 勿主动操作。
## 关键决策
assetid 不是全局唯一，必须带 AppID/contextid。下一动作优先 Steam 上架日期，同日期才用本地请求时分秒。真实写入须同时开启两个开关；手机确认由用户完成。`active` 跨重启保留，`pending_confirmation` 不缓存。
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
