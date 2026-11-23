# 联名积分与出资结算 数据字典与设计说明

适用页面：`/cobrand`。子系统源码位于 `app/cobrand/`，与原「积分批次账」七模块共用 FastAPI 进程，
但使用独立引擎、独立 SQLite 表（`cobrand_settlements`、`cobrand_active`）与独立数据卷，互不影响。

## 1. 关键设计决策：沿用原消费出资构成，拒绝当前活动池承接

联名积分进入会员同一余额后，单从余额无法区分出资方。本系统在**每笔消费落账瞬间冻结出资构成**
（品牌方/商场方各承担多少积分，附事件指纹 `evidence`），退款时严格按该构成逆向恢复：

- 方案 A（采用）：退款按原消费构成等比拆分（最大余数法，整数零头按余数大小、再按 brand→mall 归属），
  同步恢复会员带标签额度并释放对应出资池预算。每一份恢复都能逐笔追溯到原出资池。
- 方案 B（否决）：先恢复会员可用积分、再由当前活动池承接。该方案切断退款与原消费的追溯链，
  会把非原出资方的预算记为支出，造成一方虚增、一方透支，财务无法对账。

## 2. 事件模型（输入 JSON，上限 2MB）

| 字段 | 含义 | 校验 |
|---|---|---|
| id | 事件编号 | 非空字符串；同号重复事件整笔隔离（`DUPLICATE_ID`），不进账本、不占预算 |
| type | BUDGET / ISSUE / SPEND / REFUND | 其他值隔离 `BAD_TYPE` |
| at | 事件时刻 | 必须带时区偏移，统一 UTC 排序 |
| pool | 出资池 | 仅 `brand` / `mall`；每池预算仅可设定一次 |
| member | 会员号 | 字符串，前导零保留 |
| points | 积分 | 正整数 |
| shares | 发放出资额 | `{brand, mall}` 非负整数且之和等于 points |
| ref | 原消费号 | 退款必填，须指向同会员已成功消费 |

同一时刻排序优先级：BUDGET → ISSUE → SPEND → REFUND，同类按编号。

## 3. 结算口径与守恒式

每个出资池输出：`budget`（预算）、`gross_spend`（累计支出）、`released`（退款释放）、
`net_spend = gross_spend − released`、`remaining = budget − net_spend`（结余）。

引擎内置五项不变量（`checks`，任一不成立则 `balanced=false`，禁止发布）：

1. `pool_conservation`：每池净支出 = 消费占用合计 − 退款释放合计。
2. `no_overdraft`：双方 `remaining ≥ 0`。消费落账前预检，任何一方将透支则整笔隔离
   （`BUDGET_OVERDRAFT`），双方预算都不被占用。
3. `refund_restoration`：退款释放合计 = 会员恢复合计 = 退款积分合计（退款当天恢复）。
4. `funding_basis`：每笔退款的两池拆分合计等于退款额，且 `basis` 指向某笔原消费冻结依据。
5. `member_balance`：会员带标签余额与池账同口径，发放 = 可用 + 净支出。

隔离原因码：`BAD_SHARE`、`DUPLICATE_ID`、`DUPLICATE_BUDGET`、`INSUFFICIENT_POINTS`、
`BUDGET_OVERDRAFT`、`REFUND_CAP`（累计退款超原消费）、`REFUND_REFERENCE`（引用非本会员/不存在消费）、
`UNKNOWN_MEMBER`（无发放记录会员的消费/退款）、`BAD_TIME`、`BAD_NUMBER`、`MISSING_REF` 等。
隔离事件不改任何余额与预算。

## 4. 幂等、发布与防篡改

- 结算号 `cb-<sha256(输入 + 规则版本)[:16]>`：同输入同规则版本生成同一结算号，重复提交只保留一条。
- `POST .../rerun` 读取不可变 `input_snapshot` 重算；规则未变则结算号不变，**重跑同一退款不重复占用预算**。
- 发布 `POST .../publish` 携带当前 `revision`（乐观锁）：服务端先用输入快照重算并与库内核心账证
  （pools/members/spends/refunds/quarantine/checks/partners）逐项比对，不一致返回 409 `EVIDENCE_TAMPERED`
  ——原始出资依据被覆盖后无法恢复，发布中止；守恒不过返回 422；版本陈旧返回 409 `STALE_VERSION`。
- 发布在单事务内完成：切换 `cobrand_active` 当前生效指针、旧版置「已归档」、revision+1。
  任何失败都回滚并仅把该结算单标记「发布失败：…」，**旧发布版保持生效**。
- 证据包 `GET .../export/{id}` 为单一 zip 下载入口，含 `manifest.json`、`settlement.json`
  （原消费冻结依据、退款来源、会员恢复额、双方结余、隔离清单、守恒校验）与 `input_snapshot.json`。

## 5. 页面展示边界

`/cobrand` 只展示三类信息：会员恢复额（含当前可用积分）、双方预算结余、发布状态。
消费/退款逐笔出资明细、隔离事件等内部账证仅通过 API 与证据包提供给财务复核，页面不展开，
也不出现任何其他营销活动内容。

## 6. API 一览

| 方法与路径 | 作用 |
|---|---|
| GET `/cobrand` | 结算页面（无已发布版本时自动生成演示结算单） |
| POST `/api/cobrand/settlements` | 生成/复用结算单，body 可带 `json_text` |
| GET `/api/cobrand/settlements` | 结算单历史 |
| GET `/api/cobrand/settlements/{id}` | 结算单详情（完整账证） |
| POST `/api/cobrand/settlements/{id}/rerun` | 按不可变快照重算 |
| POST `/api/cobrand/settlements/{id}/publish` | 乐观锁发布，body `{"revision": n}` |
| GET `/api/cobrand/active` | 当前生效发布版 |
| GET `/api/cobrand/export/{id}` | 证据包 zip 下载 |

## 7. 演示数据核对（sample_data/cobrand_demo.json）

14 笔采信、9 笔隔离。结果：品牌池预算 500、净支出 64、结余 436；商场池预算 25、净支出 16、结余 9；
会员 M001 恢复 30、可用 60；M002 恢复 10、可用 10。退款 R2（原消费 60/40，退 10）按最大余数法
拆为品牌 6、商场 4，逐笔可追溯至 S3 的冻结依据。
