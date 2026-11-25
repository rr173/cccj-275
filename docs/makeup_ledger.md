# 补差账本（电费换单的迟到接收）

园区会先收到**后生效**的电费换单、几天后才收到**更早生效**的换单。补差账本按供电方的
**生效序号**而不是接收日期裁定换单归属，按**实际接收批次**给出本次需要补记或冲回的
**日差额**，并把每批接收后财务当时看到的完整结果冻结留存，重开后仍可查询。

- 只处理**电能费用**（结算引擎中的电量 × 电能单价金额）；显式标注为基本电费/需量电费/
  力调调整等非电能类的行会被跳过，绝不并入电能补差。
- 账本是结算项目的增量能力，规范实现位于 `meter_allocator.py`（`MakeupLedger` 及
  `open_makeup_ledger` / `receive_exchange_batch` 等函数），`makeup_ledger.py` 是等价的
  独立入口与再导出模块。**原 `settle()` 入口、CLI 与输出逐字段不变。**
- 所有金额在账本里都是**整数分**；系统金额保留 `Fraction` 精确值（元分数串与折成分后的
  分数串），不使用浮点。

测试：

```bash
python3 -I -m unittest discover -s /workspace/tests -v
# 仅账本：
python3 -I -m unittest discover -s /workspace/tests -p 'test_makeup_ledger.py' -v
```

---

## 1. 端到端 30 秒

```bash
# 1) 开账：固定结算输入 + 无重叠原账单 -> 账本状态文件
python3 -I /workspace/meter_allocator.py \
  --ledger-open   /workspace/examples/ledger_opening.json \
  --ledger-store  /tmp/park_ledger.json

# 2) 接收第一批（生效序号 8 的换单）
python3 -I /workspace/meter_allocator.py \
  --ledger-receive /workspace/examples/ledger_batch_1.json \
  --ledger-store   /tmp/park_ledger.json

# 3) 几天后收到更早的生效序号 5 —— 第 8 号单会由“已生效”改判“待核”，
#    本批日差额自动给出补记/冲回。
python3 -I /workspace/meter_allocator.py \
  --ledger-receive /workspace/examples/ledger_batch_2.json \
  --ledger-store   /tmp/park_ledger.json \
  --ledger-postings-csv /workspace/examples/ledger_postings.csv

# 查询：当前视图 / 某一批冻结视图 / 批次清单
python3 -I /workspace/meter_allocator.py --ledger-view \
  --ledger-store /tmp/park_ledger.json
python3 -I /workspace/meter_allocator.py --ledger-view R1-2025-03-10 \
  --ledger-store /tmp/park_ledger.json
python3 -I /workspace/meter_allocator.py --ledger-history \
  --ledger-store /tmp/park_ledger.json
```

同样的参数也可用于 `python3 -I /workspace/makeup_ledger.py ...`。

Python API：

```python
import meter_allocator as ma

ledger = ma.open_makeup_ledger(
    settlement=settlement_input_dict,   # 会在内部调用 settle()
    # 或 settlement_report=settle(root) 的现成报告
    original_bills=[...],                # 也可用 bills=
    store_path="/tmp/park_ledger.json",  # 给了就原子落盘
)
result = ledger.receive_batch([exchange_order, ...], batch_id="R1")
ledger.save()

ledger.view("R1")          # 该批接收后财务所见的冻结视图
ledger.attribution("R1")   # 有效账单归属（范围/日 -> 行号）
ledger.apportionments("R1")# 已入账日分摊 + 系统日金额 + 日差额
ledger.get_batch("R1")     # 整批留存记录（含 postings、改判与理由）
ma.MakeupLedger.load("/tmp/park_ledger.json")  # 重开
```

---

## 2. 开账

开账输入（Python 关键字或 `--ledger-open` JSON 文档）：

- 固定结算输入：`settlement=`（输入根对象，内部执行 `settle()`）、`settlement_report=`
  （现成结算报告），或开账文档里的 `settlement_input_path` / `baseline`；
- 无重叠原账单：`original_bills=` / `bills=`。每行字段：
  - 唯一行号：`line_id` / `line_no` / `line_number` / `行号`；
  - 结算范围：`scope_id` / `scope` / `settlement_scope_id` / `结算范围`；
  - **半开**计费日区间 `[day_from, day_to)`（结束日不计），支持
    `start_day/end_day`、`start_date/end_date`、`开始日期/结束日期`、数组
    `interval:[a,b]` 等别名；`end_inclusive:true` 时结束日按含端点处理；
  - 非负整数分金额：`amount_cents` / `amount_fen` / `金额分`；也接受
    `amount_yuan`（十进制/分数元，必须恰好为整数分）或智能 `amount`
    （int 视为分，小数字符串视为元）。

### 2.1 哪些日期可开账

对每个 `(scope, billing_day)`，从结算报告建立“结算依据”：

1. 当天必须至少有一个 `status == "verified"` 的结算格
   （`verification.control_checks`，且 `sum_matches_control`）；
2. 这些格所属连通范围组件必须 `status == "settled"`，组件的**全部相关格**都 verified；
3. 组件 `segment_ids` 中的**全部读数段**的 segment check 都 `verified`；
4. 当天各费率格 `amount_fraction` 合计（精确 `Fraction`，元）×100 后的系统金额
   必须非负。

任一原账单行覆盖了不满足条件的日期 -> 整个开账被拒绝
（`LedgerOpeningRejected.report["rejections"]`，代码
`bill_line_covers_non_openable_days` / `no_settlement_basis`），不产生账本。

此外，基线 `verification.global_issues` 中只要存在**无法归属到任何 scope** 的结算阻断
证据（没有 `scope_id`、也不能经换表凭证/表计绑定/切片表计归属到范围，例如引用了不存在
读数的凭证），立即拒绝开账，代码
`unattributable_settlement_blocking_evidence`。可归属到范围的证据保留在
`scope_blockers`，并通过上面的格/段核验门控自然体现。

### 2.2 每行金额按精确系统金额比例分摊到分

设行金额 \(A\in\mathbb Z_{\ge0}\)，区间各日精确系统金额（分，`Fraction`）为 \(w_d\)：

\[
q_d=A\frac{w_d}{\sum w},\qquad a_d^{(0)}=\lfloor q_d\rfloor
\]

剩余分数 \(R=A-\sum_d a_d^{(0)}\) 按小数余量 \(q_d-\lfloor q_d\rfloor\) 从大到小，
每日补 1 分；余量相同则较早日期优先。若区间内系统金额全为 0，则按等权
（\(w_d=1\)）处理后再走同一最大余量规则。实现：
`ledger_apportion_by_weights()`。分摊总额严格守恒为 \(A\)。

### 2.3 日差额

系统日金额四舍五入到分（half-up，`.5` 向上）：

\[
s_d=\operatorname{round}_{1/2\uparrow}\bigl(100\cdot
\text{systemAmountYuan}_d\bigr)
\]

\[
\operatorname{diff}_d=a_d-s_d
\]

开账批 `OPENING`（可用 `opening_batch_id` 改名）冻结初始归属、分摊、日差额与全部
结算依据。示例中 `feasible_crossday_input.json` 的 S1 在 2025-01-01/02 系统精确金额为
`200/3`、`100/3` 分，四舍五入为 67、33；100 分原账单按 2:1 分摊也恰为 67、33，日差额 0。

---

## 3. 换单与生效次序裁定

每张换单字段（同样支持大量别名与中文键）：

- 唯一编号：`exchange_id` / `order_id` / `voucher_id` / `换单编号` …；
- 唯一生效序号：`effective_seq` / `effective_sequence` / `seq` / `生效序号`（整数）；
- `scope_id` + 半开区间 `[start_day,end_day)`（必须在开账域内，见下）；
- 要冲红的旧行：`old_line_ids` / `red_flush_line_ids` / `old_lines` / `冲红行号`
  （id 列表，或带 `line_id` 的对象列表，也接受单个 `old_line_id`）；
- 新账单行：`new_lines` / `replacement_lines` / `新账单行`。每行有唯一行号、半开子区间
  与非负整数分金额；这些子区间必须**逐日不重叠且恰好覆盖换单区间**。新行可不写
  `scope_id`，自动继承换单范围。

### 3.1 结构合法性（不合法整批不接收）

缺编号/序号/区间、序号不是整数、金额为负、新行重叠或不能完整覆盖区间、新行号在**整个
账本**（含已 `待核` 单声明的新行、原账单行）重复、新生效序号已被另一张单占用等，都会抛
`LedgerBatchRejected`（携带结构化 `report["rejections"]`），账本与状态文件均不变。
一次接收可以是一个批文档 `{"batch_id":...,"exchanges":[...]}`、一个数组或单张换单；
批内任一单结构/唯一性不合法，整批拒绝（原子）。

换单区间必须落在开账域内。默认 `out_of_opening_policy="reject"`（违反时整批拒绝，
代码 `exchange_interval_outside_opening`）；也可开账时选 `"pending"`，把这类单永久判为
待核且不产生日变更。

### 3.2 裁定规则（按生效序号从开账状态折叠）

对序号 \(s\) 的换单 \(X\)，在序号小于 \(s\) 的全部**已生效**换单折叠后的归属状态上检查：

1. 区间内每一天的当前归属行都必须属于 `old_line_ids`；
2. 每个列出的旧行号都必须至少命中一天。

两条都满足 -> `effective`（已生效，中文“已生效”）：区间内归属整体替换为新行，新行按
第 2.2 节同一规则独立分摊一次（不可变底账）。否则整单
`pending_verification`（待核），**没有任何日变更**，理由逐日列出
（`day_not_attributed_to_listed_old_line`，含当天实际归属行；以及
`old_line_has_no_hit_day`，并标注 `line_known_to_ledger=false` 表示该旧行尚未收到）。

**旧行剩余日期的已分金额不变**：每张账单行（原行或新行）的日分摊表在其进入账本时一次
算定且永不重算；换单只切换其区间内“当前启用哪张行的哪一天分摊”。因此只替换区间内的
归属/金额，区间外原行日期保留开账时的具体分值，而不是把剩余金额重新摊平。

“旧行可尚未收到”：旧行号不必已存在于 `line_catalog`。典型链条是高序号单先到、要冲红
低序号单创建的新行 -> 待核；低序号单日后收到并生效后，高序号单可改判生效。

---

## 4. 增量重裁定与批次补记/冲回

每次接收一个实际批次（可含多张单）：

1. 先完成全部解析/幂等/唯一性校验，任何拒绝都在改动前抛出 -> 本批整体不生效；
2. 登记新单与其不可变新行分摊；
3. 计算影响集：从本批新单出发，仅沿**同 scope、半开日期区间相交、且目标序号更大**的边
   做闭包，得到“新单 + 能通过日期重叠传递影响的较晚换单”。不相交、跨 scope、序号更早的
   历史单完全不重扫；
4. 把影响集中当前已生效的旧单按序号**降序回滚**（用裁定时留存的
   `prior_line_by_day` 历史底账恢复前归属），再按序号**升序重新裁定/应用**；
   影响集之外的已生效单在不相交日期上保持应用状态，与重折叠严格等价；
5. 比较影响日期集合在接收前、后的当前日差额：

\[
\operatorname{postingDelta}_d
 =\operatorname{diff}^{after}_d-\operatorname{diff}^{before}_d
 =a^{after}_d-a^{before}_d
\]

（系统日金额固定，故二者相等。）`delta_cents > 0` 为 `supplementary`（**补记**），
`< 0` 为 `reversal`（**冲回**，绝对值见 `reversal_cents`）。每条分录保留
前后行号、前后分摊、系统金额、前后日差额与带符号 delta；行归属变了但金额相同的日进入
`daily_changes`（delta 0）供审计，只有非零 delta 进入 `postings`。

返回/留存内容还包括：

- `new_order_rulings`：本批新单的裁定与理由；
- `changed_adjudications`（别名 `readjudications`）：因此次接收**改判**的既有单，
  含 `before_status -> after_status`、新旧理由、逐日 bad-day 证据、相应
  `old_line_records`（未收到的旧行标 `in_ledger:false`）与 `new_line_records`；
- `reaffirmed_adjudications`：经重叠重裁但裁定未翻转（仅命中细节变化）的单；
- `propagation_edges`：重叠传播边与具体重叠日期；
- `considered_order_ids`：本次实际重裁的单（可据此验证没有重扫无关历史）。

`MakeupLedger.verify_incremental_matches_full_refold()` 是审计自检：真正从开账状态把
全部已收到换单按序号完整折叠一遍，比较每份裁定、归属与日分摊。测试用 14 张随机单 ×
6 种乱序到达排列验证增量算法与全量重折完全一致。

各批分录按日跨批相加严格望远镜等于“当前分摊 − 开账分摊”；相邻批次视图的
`current_daily_difference_cents` 之差等于该批 `batch_posting_delta_cents`。

---

## 5. 幂等、唯一性与重开查询

- 同一 `exchange_id` 重送**语义相同内容**（键别名/键序不同也算相同，按规范化内容哈希）
  -> `status:"unchanged"`，不产生新批次；同号但内容更改 ->
  `exchange_id_reused_with_changed_content` 整批拒绝。
- 同一 `batch_id` 重放同一组单 -> 返回留存批次并标 `idempotent_batch_replay`；
  `batch_id` 已用于不同内容 -> 拒绝。
- 每次成功接收在状态中追加一个不可变批次记录，其 `view` 即**该批后财务所见**：
  有效归属、完整日账（含结算依据）、行目录、当时各单裁定、本批分录/改判与合计。
- 落盘为单个 JSON，临时文件 + `os.replace` 原子替换；一次“接收 + 改判 + 补记 + 落盘”
  整体生效。重开：

```python
ledger = ma.MakeupLedger.load("/tmp/park_ledger.json")
ledger.list_batches()                 # 或 ledger.history()
ledger.view("R1-2025-03-10")          # 冻结视图；"latest"/None=最新；0/"OPENING"=开账
ledger.get_batch("R1-2025-03-10")     # 含 view 的完整批次记录
ledger.attribution(batch_id, scope_id="S-PARK")
ledger.apportionments(batch_id)
ledger.postings_history()             # 跨批扁平分录
ledger.get_order("EX-8")              # 含完整 ruling_history
```

CLI：`--ledger-view [BATCH]`（不带批次号=最新；也可用 `--ledger-batch`）、
`--ledger-history`、`--ledger-postings-csv out.csv`。

---

## 6. 输出对象速查

| 对象/键 | 含义 |
| --- | --- |
| `makeup_ledger_state`（状态文件） | `basis`、`domain_days`、`lines`、`attribution`、`daily`、`orders`、`seq_index`、`batches` |
| `makeup_ledger_opening` | `status: opened/rejected`、`opened_scope_days`、拒绝理由 |
| `makeup_ledger_batch_result` | `status: accepted/unchanged/rejected`、`postings`、`changed_adjudications`、`snapshot`、`totals` |
| `makeup_ledger_view` | `effective_attribution`（别名 `bill_attribution`）、`daily_ledger`（别名 `daily_apportionments`/`daily_differences`）、`line_catalog`、`exchange_orders`、`settlement_basis` |

状态值同时给英文规范 token 与中文标签：`effective/已生效`、
`pending_verification/待核`、`supplementary/补记`、`reversal/冲回`。

为兼容不同命名习惯，模块还提供 `MakeupDifferenceLedger`、`TrueUpLedger`、
`DifferenceLedger`、`AdjustmentLedger`、`open_ledger`、`receive_batch`、
`load_ledger`、`run_makeup_ledger_workflow` 等别名；类方法有
`receive/accept_batch/intake_batch/post_batch`、`snapshot/as_of/state_at` 等别名。

---

## 7. 手算叙事（`examples/ledger_*`）

结算输入 `ledger_settlement_input.json` 有 5 个计费日，系统金额恰为
100/200/300/400/500 分。原行 `BL-A=[03-01,03-04)` 600 分 -> 100/200/300，
`BL-B=[03-04,03-06)` 900 分 -> 400/500，开账日差额全 0。

- **R1（03-10 接收）EX-8/seq8** 冲 `BL-A`+`BL-B`（03-03 命中 BL-A、03-04 命中 BL-B）
  -> 生效。新行 333/380 分 -> 03-03 补记 `+33`、03-04 冲回 `-20`。
- **R2（03-13 接收更早的 EX-5/seq5）** 在 03-02、03 冲 `BL-A`，480 分按 200:300 摊为
  192/288 -> 生效。重叠 03-03 使 EX-8 被重裁：03-03 已归 `N5` 而非 BL-A，且 BL-A 零命中
  -> **EX-8 由 effective 改判 pending_verification**。本批相对 R1 视图：
  03-02 `200->192` = **-8 冲回**；03-03 `333->288` = **-45 冲回**
  （前日差额 +33 -> 新日差额 -12）；03-04 `380->400` = **+20 补记**。

R1 的冻结视图在 R2 之后、重开之后仍显示 EX-8 effective、03-03=333；最新视图则显示其待核、
03-03=288。见 `ledger_opening_result.json`、`ledger_batch_1_result.json`、
`ledger_batch_2_result.json`、`ledger_postings.csv`。
