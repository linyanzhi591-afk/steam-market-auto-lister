# 开发版交接

## 当前分支
`develop`，目录 `D:\tools\steam-market-auto-lister-dev`；稳定版目录为 `D:\tools\steam-market-auto-lister`，勿主动改动或推送。

## 最新提交
`f0d4ccc` 打印完整运行的饰品操作明细。

## 已完成内容
- 库存、挂单和待确认使用 `(appid, contextid, assetid)` 复合键，旧 SQLite 自动迁移。
- 完整运行重建后从 `listing_submissions` 与调价历史恢复请求时间、阶段、策略和下一动作，避免退回第一阶段 trend。
- 新上架支持批量自定义价格；新任务与价格异常按同情况折叠；库存数量仅显示数字。
- `run-full-once.bat` 遇价格异常会打开本地网页并定位“价格异常待确认”；启动时保留 `price_review`。自定义价格旁可打开对应 Steam 市场。
- `run-full-once.bat` 完整运行时会在 Steam 操作成功后同步打印饰品明细：超时调价分别打印下架与重新上架，普通任务打印新上架；日志包含饰品名和买家支付价格，调价下架还包含原价与目标价。业务判断和执行顺序未改动。

## 验证
`.venv\Scripts\python.exe -m pytest -q`：58 项通过。

## 未完成事项
无已确认的功能待办。

## 已知问题
Steam 为非公开网页接口，页面变化可能导致解析失效；缺少 Listing ID 的同名物品只能名称兜底，存在误匹配风险；旧主键覆盖的库存需重新同步。

## 关键配置
所有配置以前缀 `STEAM_LISTER_` 读取；真实写入须同时设置 `DRY_RUN=false` 与 `ALLOW_MARKET_WRITES=true`。常用项还有 `DEFAULT_CURRENCY`、请求延迟/超时/重试、库存分页、批次及单件价格上限。

## 下一步建议
先用低价值饰品验证真实上架与手机确认；若 Steam 页面改版，优先检查库存、在售和市场价格解析。
