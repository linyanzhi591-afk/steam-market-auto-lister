# 开发版交接

## 当前分支
`develop`，目录 `D:\tools\steam-market-auto-lister-dev`；稳定版目录为 `D:\tools\steam-market-auto-lister`，勿主动改动或推送。

## 最新提交
本次提交：按运行期状态筛选全部非黑名单可出售库存。

## 已完成内容
- 完整运行第 3 步不再按数据库开放任务跳过库存；除黑名单外，仅跳过本次运行内等待手机确认和调价临时下架的资产。
- 等待手机确认使用 `ListingManager` 运行期资产集合保护；下一次完整运行会清空，不再从未匹配的历史提交跨运行恢复 `pending_confirmation`。
- 调价临时下架使用独立运行期资产集合，既不参与第 3 步普通计划生成，也不在调价失败后进入第 4 步普通新上架路径。
- 启动缓存现在仅保留已确认 `active` 挂单；旧 `price_review` 会在新运行中按最新库存和价格重新生成。
- 本地同步候选除按任务 ID 保留最新提交外，还按 `(appid, contextid, assetid)` 仅保留最新一条；`reprice_history` 也不会为已有候选的同一资产重复补充任务。
- `sync_active_listing_groups` 遇到提交记录仍指向已清理的旧任务、但同一资产已有开放任务时，复用现有开放任务并修正 `listing_submissions.listing_record_id`，不再重复插入同一 `(appid, contextid, assetid)`。
- 库存、挂单和待确认使用 `(appid, contextid, assetid)` 复合键，旧 SQLite 自动迁移。
- Steam 当前在售同步仅按 `(appid, contextid, market_hash_name, buyer_price_minor)` 分组并比较数量；Listing ID、Asset ID 和上架日期不参与匹配、确认或策略恢复。
- 分组内从每条任务最新的 `listing_submissions` 恢复策略、策略配置与阶段；旧数据缺少提交记录时由 `reprice_history` 按同一分组键补齐，避免错误回退为 `trend`。
- 同步记录带有 `matched`、`external`、`pending_match` 分类；Steam 多出的数量作为外部挂单，本地多出的数量保持待匹配，数量不一致会显式写入状态说明。
- 浏览器当前在售表按四字段键聚合，显示本地、Steam、已匹配、待匹配、外部五类数量以及策略/阶段，并标出数量不一致。
- 新上架支持批量自定义价格；新任务与价格异常按同情况折叠；库存数量仅显示数字。
- `run-full-once.bat` 遇价格异常会打开本地网页并定位“价格异常待确认”；启动时保留 `price_review`。自定义价格旁可打开对应 Steam 市场。
- `run-full-once.bat` 完整运行时会在 Steam 操作成功后同步打印饰品明细：超时调价分别打印下架与重新上架，普通任务打印新上架；日志包含饰品名和买家支付价格，调价下架还包含原价与目标价。业务判断和执行顺序未改动。

## 验证
- 真实数据库临时副本模拟“新运行启动、库存回填、Steam 当前在售为空”：非黑名单可出售 215 件，可生成计划 215 件，开放任务冲突 0；临时副本已删除。
- `.venv\Scripts\python.exe -m pytest -q tests\test_database.py tests\test_listing_manager.py`：37 项通过。
- `.venv\Scripts\python.exe -m ruff check app\core\database.py app\services\listing_manager.py tests\test_database.py tests\test_listing_manager.py`：通过。
- 真实 `data/steam_lister.sqlite3` 的临时副本执行 `sync_active_listing_groups([])`：处理 249 条记录，无唯一约束错误；临时副本已删除。
- `node --check app\static\app.js`：通过。

## 未完成事项
无已确认的功能待办。

## 已知问题
- Steam 为非公开网页接口，页面变化可能导致解析失效；旧主键覆盖的库存需重新同步。
- 本方案刻意不确认某个本地资产对应哪一条 Steam 挂单；Listing ID 仅保留为后续撤单所需的操作载体和展示字段。
- 同组本地数量多于 Steam 数量时，只能按本地提交顺序保留相应数量为已匹配，其余为待匹配，不能推断具体资产对应关系。

## 关键配置
所有配置以前缀 `STEAM_LISTER_` 读取；真实写入须同时设置 `DRY_RUN=false` 与 `ALLOW_MARKET_WRITES=true`。常用项还有 `DEFAULT_CURRENCY`、请求延迟/超时/重试、库存分页、批次及单件价格上限。

## 下一步建议
先用低价值饰品验证真实上架与手机确认；若 Steam 页面改版，优先检查库存、在售和市场价格解析。

## 2026-07-31 目标饰品操作审计

- 审计范围：`data/steam_lister.sqlite3` 及其迁移备份、`app/services/pricing.py`、`app/services/listing_manager.py`；未读取旧任务聊天记录。
- `P250 | Constructivist (Minimal Wear)`（中文：P250 | 建构主义者（略有磨损））：12 个资产；`reprice_history` 36 次，均为 `age_timeout`，阶段 0 → 1，卖家到账/买家支付从 `6.00/8.00` 降到 `5.60/7.60`；对应 `listing_submissions` 12 次，最终均记录为 `submitted`，当前重新同步后的在售记录为 12 件、`6.00/8.00`，`price_source=external`。
- `AUG | Steel Sentinel (Field-Tested)`（中文：AUG | 钢铁哨兵（久经沙场））：9 个资产；`reprice_history` 27 次。早期 9 次为 `6.00/8.00` → `5.60/7.60`，后续 18 次为 `5.00/7.00` → `4.65/6.65`；对应重新上架提交 9 次，当前在售 9 件、`5.00/7.00`，`price_source=external`。
- `P250 | Constructivist (Factory New)`（中文：P250 | 建构主义者（崭新出厂））：5 个资产；`reprice_history` 15 次。价格轨迹为 `35.66/41.00` → `33.88/38.95`（5 次）、`33.05/38.00` → `31.87/36.64`（3 次）、`33.92/39.00` → `32.22/37.05`（2 次）、`33.05/38.00` → `31.58/36.30`（2 次）、`32.19/37.00` → `31.58/36.30`（3 次）；当前在售 5 件、`32.19/37.00`，`price_source=external`。
- `Nova | Sausage Fest (Battle-Scarred)`（中文：新星 | 香肠地狱（战痕累累））：当前数据库及迁移备份均无库存、挂单、上架提交、调价历史、审计日志或价格历史记录，不能据此确认曾经发生过操作或价格。
- 原因：所有已记录调价均由 `age_timeout` 触发，进入默认策略阶段“稳健出售”（`robust_median`）；阶段配置使用近 30 天清洗成交点、时间加权中位价，并要求至少 24 个价格点。最终买家支付价还受最低价、最低费用和原价最多下降 5% 的保护，再按 Steam 费用反算卖家到账价。当前 `price_history` 表为空，所以无法还原每次计算使用的实际成交点、中位价和当前最低价，只能确认数据库落盘的结果价格及策略规则。
- 本任务没有展开第二个独立功能；代码未修改，仅更新本交接文档。验证命令：`.venv\\Scripts\\python.exe -m pytest -q`。

## 2026-08-02 修复再次上架被判定为外部

- 原因：当前在售同步按饰品名和买家支付价分组；同一资产手动下架后再次由程序上架且价格变化时，无法匹配历史程序提交，被写成 `external`，因此 `next_action_at` 为 `NULL`，界面显示“未设置”。
- 修复：同步时优先使用 Steam 返回的 `(appid, contextid, assetid)` 查找最近有效的程序提交；若找到，即使当前价格不同，也恢复策略、阶段和程序匹配状态，并按当前实际买家支付价保存下一次动作时间。
- 没有任何历史程序提交来源的挂单仍保持外部挂单，不会被误接管。
- 新增回归测试覆盖“同一资产重新上架但价格变化”的场景。
- 验证：`.venv\\Scripts\\python.exe -m pytest -q`（65 passed）；相关 Ruff、`node --check app\\static\\app.js` 和 `git diff --check` 均通过。

## 2026-08-02 处理已离开在售列表的本地提交

- 本地提交数量多于 Steam 当前在售数量时，不再把差额视为待匹配异常；多出的提交统一释放，相关 `listing_submissions` 标记为 `sold`，后续同步不会重复计入本地数量。
- 饰品仍在库存时，下一次完整运行可按正常流程重新生成上架计划；饰品已不在库存时，不会重新上架。
- Steam 在售数量多于本地提交数量的情况仍按外部挂单处理。
- 验证：全量测试 `65 passed`，Ruff、前端语法检查和 `git diff --check` 均通过。
