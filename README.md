# Steam 饰品自动上架

本地运行的 Steam 社区市场批量上架与自动重定价工具。程序扫描账号中的全部库存上下文，筛选可出售饰品，使用最近 30 天成交数据定价，提交挂单，并在超时未售后撤单、切换策略和重新提交。Steam Guard 上架确认始终由用户在手机端完成。

> 当前版本：`v1.0.0`。真实市场接口属于 Steam 网页端非公开能力，可能随 Steam 页面或接口调整而失效。第一次使用真实写入前必须用一件低价值饰品验证。

## 已实现

- Steam 官方页面登录，不通过自制表单收集密码。
- 浏览器会话持久化，下次启动自动恢复。
- 自动发现库存游戏和上下文，完整分页扫描。
- 筛选所有标记为 `marketable` 的资产。
- 最近 30 天成交价格和成交量采集、缓存。
- IQR 异常价格清洗和成交量加权。
- 稳健中位价、趋势价、市场跟随价、快速出售价。
- CNY 人民币和 INR 印度卢比。
- Steam 费用与游戏发行商费用的整数反算。
- 上架计划预览和同一资产幂等保护。
- 可出售库存按同名物品折叠，支持单选和全选后生成计划。
- 持久化饰品黑名单；黑名单物品不参与库存展示、价格同步或新计划。
- 独立设置界面，集中管理钱包币种、策略方案和黑名单。
- 自由组合多阶段策略：算法、百分比/固定调价、绝对底价、中位价保护、持续时间和到期动作均可配置。
- 同步 Steam“我的在售”，自动导入并跟踪不是由本程序创建的现有挂单。
- 新上架任务与 Steam 当前在售分区显示；当前在售支持多选、全选并按指定策略调价。
- Steam 提交失败时在任务结果栏保留具体原因，失败任务可直接重试。
- 登录或自动恢复 Steam 后检测钱包币种，支持自动选择 CNY/INR。
- 每次启动清理库存、价格缓存和未提交计划；保留真实挂单跟踪、黑名单及设置。
- 待手机确认、在售、售出、暂停和失败状态。
- 超时撤单、重新拉取行情、切换策略并自动重新提交。
- 最低卖家到账、高价值限制、批次上限和名称排除。
- SQLite 持久化、审计日志和后台调度。
- 双重真实市场写入开关和执行确认文本。

## 安全默认值

- 仅监听 `127.0.0.1`。
- 默认 `dry-run=true`。
- 默认 `allow-market-writes=false`。
- 不保存 Steam 密码、Steam Guard 密钥或恢复码。
- 登录资料仅保存在本机 Playwright Chromium 配置目录。
- 单批默认最多处理 2000 件，可在界面调整；第一次真实执行仍应只选择一件。
- 单件买家支付价默认不得超过 1000.00 钱包货币单位。
- 同一资产只能存在一个开放任务。
- 真实提交后仍需在 Steam 手机客户端手动确认。

## 安装

需要 Python 3.11 或更高版本。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m playwright install chromium
```

## 演练模式启动

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

打开 <http://127.0.0.1:8765>，依次执行：

1. 点击“登录 Steam”，在弹出的 Steam 官方窗口登录。
2. 点击“同步库存”。
3. 点击“同步30天价格”。
4. 勾选需要出售的库存，可使用表头复选框全选。
5. 选择策略方案和最低到账金额。
6. 点击“为选择的库存生成计划”并检查价格。

库存行上的“加入黑名单”会隐藏该名称的所有同类资产，并暂停其尚未提交的计划。可在“饰品黑名单”区域移除。

“设置”页可修改默认币种、管理黑名单，并新建、复制、编辑或删除策略方案。Steam 登录成功后会优先按钱包实际币种更新设置。

“同步当前在售”会从 Steam“我的在售”分页读取全部挂单。程序启动前或在 Steam 网页端创建的挂单也会导入本地任务列表；重复同步不会重复创建。

当前在售的调价会先撤销原挂单，再按选择的策略重新上架，因此仍需前往 Steam 手机客户端确认。为避免意外撤单，只有手动勾选的当前在售会被调整。

演练模式不会向 Steam 提交挂单或撤单。

## 启用真实市场写入

先关闭程序，然后在同一个 PowerShell 窗口设置：

```powershell
$env:STEAM_LISTER_DRY_RUN = "false"
$env:STEAM_LISTER_ALLOW_MARKET_WRITES = "true"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

点击“执行所有待提交计划”后，还必须输入：

```text
我确认执行真实市场操作
```

程序提交成功后，前往 Steam 手机客户端手动确认。建议第一次只通过 API 的 `assetids` 字段选择一件低价值饰品，不要直接批量提交。

## 自定义重定价策略

每个策略方案可包含任意数量的阶段。每个阶段可以独立设置：

- 基础价格算法：30 天趋势价、稳健中位价、市场跟随价或快速出售价。
- 百分比调价和固定金额调价，可组合使用。
- 绝对价格下限和“30 天中位价百分比”下限。
- 本阶段持续小时数。
- 到期后进入下一阶段，或直接暂停。

系统预置的默认方案为：

| 阶段 | 策略 | 在售时间 |
|---|---|---:|
| 1 | 趋势价 | 72 小时 |
| 2 | 稳健中位价 | 48 小时 |
| 3 | 市场跟随价 | 24 小时 |
| 4 | 快速出售价 | 24 小时 |

阶段可在设置页增加、删除和调整顺序。每次到期后程序会：

1. 同步当前挂单，确认仍未售出。
2. 撤销原挂单。
3. 重新获取该饰品最近 30 天行情。
4. 使用下一阶段策略计算价格。
5. 应用最初设置的最低到账保护。
6. 自动重新提交，并等待用户手机确认。
7. 最终阶段仍未售出时暂停，不再继续降价。

## 配置

所有配置使用 `STEAM_LISTER_` 前缀：

| 环境变量 | 默认值 | 说明 |
|---|---:|---|
| `STEAM_LISTER_DRY_RUN` | `true` | 演练模式 |
| `STEAM_LISTER_ALLOW_MARKET_WRITES` | `false` | 允许上架和撤单 |
| `STEAM_LISTER_DEFAULT_CURRENCY` | `CNY` | 后台任务币种 |
| `STEAM_LISTER_SYNC_INTERVAL_SECONDS` | `900` | 挂单同步周期 |
| `STEAM_LISTER_REQUEST_DELAY_SECONDS` | `1.5` | Steam 请求间隔 |
| `STEAM_LISTER_REQUEST_TIMEOUT_SECONDS` | `30` | 单次 Steam 请求超时 |
| `STEAM_LISTER_REQUEST_RETRIES` | `2` | 网络失败后的重试次数 |
| `STEAM_LISTER_INVENTORY_PAGE_SIZE` | `500` | 单页库存数量，降低 Steam 500 概率 |
| `STEAM_LISTER_MAX_BATCH_ITEMS` | `2000` | 默认批次上限 |
| `STEAM_LISTER_MAX_UNIT_BUYER_PRICE_MINOR` | `100000` | 单件买家支付上限，使用最小货币单位 |

人民币钱包应使用 `CNY`，印度卢比钱包应使用 `INR`。工具不会改变 Steam 钱包币种。

## API

主要接口：

- `POST /api/session/login`
- `POST /api/sync/inventory`
- `POST /api/sync/prices?currency=CNY`
- `GET /api/inventory`
- `POST /api/listings/plan`
- `POST /api/listings/execute`
- `POST /api/listings/reprice`
- `POST /api/sync/listings`
- `GET /api/strategy-profiles`
- `POST /api/strategy-profiles`
- `PUT /api/strategy-profiles/{id}`
- `DELETE /api/strategy-profiles/{id}`
- `POST /api/listings/process-expired`
- `GET /api/listings`

FastAPI 接口文档位于 <http://127.0.0.1:8765/docs>。

## 数据目录

```text
data/
├── steam_lister.sqlite3     # 库存、行情、任务与审计日志
└── steam-browser-profile/   # Steam 登录浏览器配置
```

退出 Steam 按钮会清除 Cookie 和网页存储。删除整个 `data` 目录会清空所有本地状态。

## 测试

```powershell
python -m pytest
python -m ruff check .
```

## 已知限制

- Steam 社区市场没有覆盖这些操作的稳定公开 API。
- Steam 页面结构变化可能影响挂单和成交识别。
- 同名饰品同时存在多个挂单时，待确认任务只能尽力匹配；首次使用应小批量操作。
- 当前费用模型采用常见的 5% Steam 费用和 10% 游戏费用。不同应用如果采用其他费率，需要扩展为按 AppID 配置。
- 真实市场结果必须以 Steam 页面和手机确认内容为准。

## 许可证

许可证尚未确定。参考项目使用 GPL-3.0；本项目仅借鉴产品思路，没有复制其源码。在确定许可证前不要公开分发。
