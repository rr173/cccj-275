# 园区抄表分时整数分配器

单文件、仅用 Python 标准库（含 `zoneinfo`）的可审计求解器：`meter_allocator.py`。

> 运行环境的 `/workspace` 初始没有业务输入文件。因此这里同时给出了明确的 JSON 输入契约、可运行样例和手算例。拿到真实输入后，只要转换成该 JSON 契约即可直接运行。

项目包含三个相互独立的能力，共用同一份输入 JSON：

1. **结算**（`settle()`，第 3、4 节）：把累计读数差按实际 UTC 时长分到计费日×费率控制格，求整数分配。
2. **计划采集窗口核对**（`reconcile_windows()`，第 5 节）：按计划窗口核对实到读数，输出实到 / 待核 / 漏采，并单列未匹配读数。核对是只读分析，**不改变任何结算结果**。
3. **修订影响分析**（`revision_impact()`，第 6 节）：输入基线结算数据与候选修订数据，输出按结算范围、计费日、费率和原始读数段归纳的新增 / 消失 / 数值变化 / 不受影响结果，并保留双方规则、绑定、读数和结算依据。只对两侧都能完整结算的范围报告影响；其余标为 `not_comparable` 并保留冲突证据。

## 1. 运行

```bash
python3 -I /workspace/meter_allocator.py \
  --input  /workspace/examples/park_input.json \
  --output /workspace/examples/park_result.json \
  --csv    /workspace/examples/park_slices.csv
```

- `-I` 为隔离模式，避免当前目录中同名模块（例如不可信数据释放出的 `json.py`）被导入。
- 成功完成分析时退出码为 `0`；输入契约错误输出 `fatal_input_error` 并以退出码 `2` 结束。
- 若存在不能结算的连通范围，进程仍写出完整证据；总状态为 `partial_settlement` 或 `not_settleable`。
- 默认结算运行**不**输出核对结果，`park_result.json` 与既有输出保持逐字段一致。

最小全可行样例：

```bash
python3 -I /workspace/meter_allocator.py \
  --input /workspace/examples/feasible_crossday_input.json \
  --output /tmp/feasible_result.json
```

### 计划窗口核对

```bash
# 只跑核对（不计算结算），输出核对报告与窗口 CSV
python3 -I /workspace/meter_allocator.py \
  --input  /workspace/examples/reconciliation_input.json \
  --reconcile-only \
  --reconcile-output /workspace/examples/reconciliation_result.json \
  --reconcile-csv    /workspace/examples/reconciliation_slices.csv

# 在正常结算报告里附加一个 planned_window_reconciliation 键（结算字段不变）
python3 -I /workspace/meter_allocator.py \
  --input /workspace/examples/reconciliation_input.json \
  --output /tmp/settlement.json --reconcile

# 结算与核对分两个文件写出
python3 -I /workspace/meter_allocator.py \
  --input /workspace/examples/reconciliation_input.json \
  --output /tmp/settlement.json \
  --reconcile-output /tmp/reconciliation.json \
  --reconcile-csv /tmp/reconciliation_windows.csv
```

核对相关参数：`--reconcile`、`--reconcile-output`、`--reconcile-csv`、`--reconcile-only`。三者中任一出现即触发核对；`--reconcile-only` 只做核对，不计算结算。

### 修订影响分析

```bash
python3 -I /workspace/meter_allocator.py \
  --baseline  /workspace/examples/park_input.json \
  --candidate /workspace/examples/revision_candidate_input.json \
  --impact-output /workspace/examples/revision_impact_result.json \
  --impact-csv    /workspace/examples/revision_impact_slices.csv
```

- `--impact` 触发影响分析；`--impact-output` / `--impact-csv` 亦触发。必须同时给出 `--baseline` 与 `--candidate`。
- 不影响分析模式下 `--input` / `--csv` 的使用；影响分析不能与 `--reconcile*` 组合。
- 正常输出退出码 `0`；任一输入契约错误输出 `fatal_input_error` 并以退出码 `2` 结束。
- 不影响既有结算与核对输出：不传影响参数时，`park_result.json` 等既有结果逐字段不变。

测试：

```bash
python3 -I -m unittest discover -s /workspace/tests -v
```

## 2. 输入契约

顶层字段：

- `rule_version`：业务规则/口径版本，原样保留。
- `billing_calendar` 或 `billing_calendars`：IANA 时区、计费日切点、节假日/调休/工作周。
- `readings`：带 UTC 偏移的累计读数。时间必须有偏移，例如 `2024-11-03T01:30:00-04:00`。
- `exchange_vouchers`：换表凭证；完整凭证应能给出旧表末值和新表起值。
- `bindings`：表计到计量点/结算范围的半开有效区间。
- `tariff_versions`：费率版本、精确有理单价、全时段或本地窗口。
- `control_totals`：结算系统给出的 `(scope_id, billing_day, rate_id) -> integer Wh`。
- `planned_windows`（可选，仅核对使用）：计划采集窗口列表，字段见第 5 节；也可用别名 `collection_windows` / `planned_collection_windows`。

单价不要传二进制 JSON float；请传十进制字符串（`"1.25"`）、分数（`"5/4"`）、整数，或 `{"num": 5, "den": 4}`。

### 换表凭证

完整例子：

```json
{
  "voucher_id": "V-1",
  "exchange_at": "2024-11-06T12:00:00-05:00",
  "old_meter_id": "M-OLD",
  "new_meter_id": "M-NEW",
  "old_end": {"ts": "2024-11-06T12:00:00-05:00", "value_wh": 1200},
  "new_start": {"ts": "2024-11-06T12:00:00-05:00", "value_wh": 0}
}
```

也支持 `old_end_reading_id` / `new_start_reading_id` 引用已有读数。缺少任一端点时，旧表最后读数到换表时刻、换表时刻到新表首读数都会成为 `pending_verification`，不会猜测差值。

## 3. 精确数学模型

对有效抄表段 \(i\)：

- 原差值 \(D_i\in\mathbb Z_{\ge 0}\)；
- 实际 UTC 时长 \(T_i\)，不用本地墙钟时长；
- 与绑定、费率版本、计费日边界求交得到交片 \(e\)，UTC 重叠时长 \(t_e\)；
- 约定均匀分布：

\[
q_e=D_i\frac{t_e}{T_i}\in\mathbb Q
\]

因为交片分割整个半开区间，所以：

\[
\sum_{e\in i}q_e=D_i
\]

每个交片只能取：

\[
y_e\in\{\lfloor q_e\rfloor,\lceil q_e\rceil\}
\]

令 \(y_e=\lfloor q_e\rfloor+z_e\)。若 \(q_e\) 为整数，则 \(z_e=0\)；否则 \(z_e\in\{0,1\}\)。

抄表段需要的向上取整个数：

\[
r_i=D_i-\sum_{e\in i}\lfloor q_e\rfloor
\]

计费日×费率控制格 \(j\) 需要的向上取整个数：

\[
c_j=C_j-\sum_{e\in j}\lfloor q_e\rfloor
\]

随后在“抄表段—控制格”二分图上求整数最大流：

- 源点到抄表段容量 \(r_i\)；
- 每个非整数交片对应一条容量 \(1\) 的段到控制格边；同一对节点间的多个交片是并行边，分别保留 `atom_id`；
- 控制格到汇点容量 \(c_j\)。

若最大流等于 \(\sum r_i=\sum c_j\)，流结果直接给出每个 \(z_e\)。否则输出局部边界、总量差和广义 Hall/最小割证据。

连通范围按“抄表段—计费日×费率控制格”的全部交片关联求并查集。换表待核、负下降、绑定/费率歧义会作为阻断节点连接其所有可能控制格，因此同一连通范围被拒绝，而其他并查集范围仍可结算。

## 4. 输出审计

`park_result.json` 包含：

- 输入 `rule_version`、引擎版本、输入文件 SHA-256、原始读数和凭证；
- 每个连通范围的状态、问题和最大流/Hall 证据；
- 每个交片的 UTC 边界、精确有理量、公式、绑定、计费日历、费率版本和单价依据；
- `segment_checks` 与 `control_checks`，独立复核段和控制格守恒；
- 金额分数字符串，例如 `15/4`，不做额外舍入。

状态含义：

- `settled`：该范围存在合法整数分配并已通过守恒复核；
- `infeasible`：数据完整，但 floor/ceil 约束下无合法分配，附冲突证据；
- `blocked_evidence`：缺凭证、无凭证负下降、绑定/费率歧义等待核证据；
- `input_error`：缺少控制总量、重复控制总量、费率配置错误等。

## 5. 计划采集窗口核对

班组按计划采集窗口核对“实到读数”。核对只读取 `planned_windows`、`readings`、`bindings`，**不修改结算**，也不需要费率或控制总量，因此可以单独运行（`--reconcile-only`）。

### 5.1 输入

```json
{
  "planned_windows": [
    {
      "window_id": "W-01",
      "point_id": "MP-01",
      "expected_from": "2024-11-03T01:00:00-04:00",
      "expected_to":   "2024-11-03T01:45:00-04:00"
    }
  ]
}
```

- 时间必须带 UTC 偏移（与结算读数同一解析规则）；计划窗口与绑定区间都是**半开区间** `[start, end)`。
- `point_id` 必填；`window_id` 缺省时按 `W1`、`W2`… 生成，重复即报 `fatal_input_error`。
- 别名：窗口开始/结束可用 `expected_start` / `expected_end` 等；列表可用 `collection_windows` / `planned_collection_windows`。
- 读数与绑定复用结算的 `readings`、`bindings`；`valid: false` 或 `status` 为 `void/invalid/cancelled/excluded` 的记录单列为排除依据，不参与匹配。

### 5.2 归属规则

一条读数只可能成为某窗口的**候选**，当且仅当：

1. 读数 UTC 时刻落在该窗口的半开区间内；且
2. 在该时刻，读数的表计存在**有效绑定**，且绑定的 `point_id` 唯一并等于窗口的 `point_id`。

在此之上：

| 情况 | 结果 |
|---|---|
| 窗口没有候选读数 | `missed`（漏采） |
| 窗口有 ≥2 条候选读数 | `pending_review`（待核） |
| 窗口唯一候选读数同时是其它窗口的候选 | 相关窗口全部 `pending_review`（不按最近时间挑选） |
| 候选读数当时绑定到多个计量点 | `pending_review`（归属不唯一） |
| 窗口恰有 1 条候选，且该读数只归属这一个窗口 | `matched`（实到） |

- **一条读数最多只会被选中一个窗口**；不做“最近时间”静默挑选。
- 没有成为任何窗口实到结果的读数进入 `unmatched_readings`，并给出原因（无有效绑定、绑定无计量点、绑定歧义、绑定到别的计量点、在窗口外、多窗口共享等）。
- 每个窗口都保留：窗口原始记录、窗口内全部读数及各自归属判定、候选读数当时的绑定行与原始记录、以及与窗口重叠的该计量点绑定行。

### 5.3 输出

`reconciliation_result.json` 顶层：

- `overall_status`：`all_windows_matched` / `attention_required` / `no_planned_windows`；
- `summary`：计划窗口数、实到/待核/漏采数、有效读数数、被选中读数数、未匹配读数数、排除读数/绑定数；
- `windows[]`：每个窗口的 `status`（含中文 `status_label`）、`expected_from/to`、UTC 边界、`selected_reading_id` 与 `selected_reading`、`candidate_readings`、`readings_inside_window`（含无法归属时的 `attribution`）、`excluded_readings_inside_window`、`overlapping_point_bindings`、`issues`、`raw`；
- `unmatched_readings[]`：每条未匹配读数及其 `reason`、`binding_state`、`active_bindings`、`contained_window_ids`、`candidate_window_ids`、`raw`；
- `excluded_readings[]` / `excluded_bindings[]`、`bindings_considered[]`、`rules`（规则原文）与 `input_audit`（含 SHA-256）。

`reconciliation_slices.csv` 是每个窗口一行的扁平摘要：窗口、状态、UTC 边界、被选读数、候选数与问题代码。

示例见 `examples/reconciliation_input.json`、`examples/reconciliation_result.json`、`examples/reconciliation_slices.csv`。

## 6. 修订影响分析

发布新读数、计量点绑定或费率修订之前，用同一份结算引擎分别对基线输入与候选修订输入求结算，再按语义身份比较两份结果。分析只读，**不改变**任一输入自身的结算结果，也不改变计划窗口核对。

### 6.1 身份与比较键

- 控制格身份：`(scope_id, billing_day, rate_id)`；
- 原始读数段身份：`(meter_id, start_utc, end_utc)`（半开区间，UTC）。

不使用生成的 `atom_id`、`component_id`、JSON 数组下标或 `readings[i]` 路径作为身份。**因此输入记录顺序变化不会制造 `added`/`removed`/`changed`**。读数时间戳本身改变则会产生旧段 `removed` 与新段 `added`，这是真实修订而非顺序噪声。

每个控制格和读数段都给出四类结果之一：

| `change` | 含义 |
|---|---|
| `added` | 候选修订中存在、基线中不存在 |
| `removed` | 基线中存在、候选修订中不存在 |
| `changed` | 两侧都存在，但目标控制量、分配电量、金额、单价、费率版本、绑定、计费日历或读数证据不同 |
| `unchanged` | 两侧都存在且上述比较字段完全一致（不受影响） |

数值差 `delta_allocated_wh` / `delta_amount_fraction` **只对双方都存在且都已复核的控制格或读数段计算**。单侧新增/消失单独列在 `added_values_not_delta` / `removed_values_not_delta`，不进入差值，也**绝不把缺失侧当作 0**。

### 6.2 可比较性（`not_comparable`）

只有当某 `scope_id` 在两侧都能形成**完整、已复核**的结算（每个控制格 `verified`、每个相关读数段 `verified`）时，才报告该范围的影响。任一侧出现以下情况即标为 `not_comparable`，保留双方冲突证据，且该范围**不输出任何数值差**：

- 未结算的连通范围（`infeasible` / `blocked_evidence` / `input_error`）；
- 阻断的读数段（无绑定、绑定歧义、费率歧义/缺失、换表端点缺失、负下降）；
- 控制总量缺失或重复；
- 该范围存在无法归入任何 `scope_id` 的全局问题（如换表凭证引用不存在的读数）。

若某侧完全**没有**该范围的数据、另一侧完整可结算，则记为整范围 `added` / `removed`（`baseline_presence` / `candidate_presence` 为 `absent` / `complete`）。若一侧有读数段而另一侧无法解析其归属，则视为**另一侧证据不足**并标为 `not_comparable`，不静默配对。

### 6.3 输出

`revision_impact_result.json` 顶层：

- `overall_status`：`all_scopes_comparable` / `partial_comparability` / `not_comparable` / `no_scopes` / `fatal_input_error`（含中文 `overall_status_label`）；
- `has_changes`、`rule_version`（含两侧 `input_rule_version` 与 `input_rule_version_changed`）、`input_audit`（两侧路径与 SHA-256）；
- `rules`：可比较性规则、身份规则、比较类别与结算依据的原文；
- `summary`：范围总数、可比/不可比数、控制格与读数段的四类合计、可比范围内的分配电量/金额差、`not_comparable_scopes`、未归属读数段数；
- `scopes[]`：每个 `scope_id` 的 `comparability`、两侧 `presence`、按 `(billing_day, rate_id)` 的 `control_cells` 与 `reading_segments`（各含 `baseline` / `candidate` 证据视图、`changes`、`delta_*`），以及按计费日归并的 `by_billing_day` 和 `rollup`；
- `evidence_catalog`：未结算连通范围的完整证据按 `range_id` 仅存一份，`scope.conflicts[].range_id` 可解析；
- `unscoped_segments`：无法归入任何范围而单列的读数段。

每个控制格与读数段都保留对应侧的可审计证据：

- 控制格：`target_wh`、`allocated_wh`、`amount_fraction`、`unit_prices`、`tariff_versions`、`binding_ids`、`control_ids`、原始控制行 `raw_control_rows`、`settlement_basis.atoms`（含 `formula_wh`、`floor/ceil`、`binding_raw`、`tariff_basis`、`calendar_id`、时区）；
- 读数段：`raw_difference_wh`、`segment_check`、`reading_evidence`（起止锚点与原始读数）、`binding_evidence`、`tariff_evidence`、`candidate_controls`。

`revision_impact_slices.csv` 是每个可比控制格（不可比范围为每个候选格）一行的扁平摘要。

示例见 `examples/revision_candidate_input.json`、`examples/revision_impact_result.json`、`examples/revision_impact_slices.csv`：候选中 `S-XDAY` 的费率单价与 `XD-R2` 读数变化使 2024-11-05 控制格 `1 -> 2 Wh`、金额 `+9/4`；`S-DST`、`S-EX` 不受影响；`S-AMB*`、`S-HALL`、`S-HQ`、`S-NEG`、`S-PEND` 因无法结算或证据不足标为 `not_comparable`。

## 7. 手算例

见 [`docs/hand_examples.md`](docs/hand_examples.md)。样例输出中的关键范围：

- `R002`：夏令时重复本地时刻；
- `R009`：跨计费日，金额 `15/4`；
- `R003`：完整换表凭证，旧段 200 Wh、新段 90 Wh；
- `R007`：无凭证负下降，隔离 `-50` Wh；
- `R008`：缺换表端点，隔离待核；
- `R006`：每个局部都能舍入，但整体无分配的广义 Hall 冲突。

计划窗口核对的逐窗口手算过程见同一文档第 5 节。修订影响分析的可比较性判定见第 6 节与 `tests/test_revision_impact.py`。

## 8. 补差账本（迟到的电费换单）

园区有时先收到**后生效**的电费换单、几天后才收到**更早生效**的一张。新增的补差账本按供电方**生效序号**（而非接收日期）裁定哪些换单入账，按**实际接收批次**交付本次需补记/冲回的**日差额**，并冻结每批接收后财务当时所见的结果，落盘后可重开查询。只处理电能费用。

完整契约、公式、API 与手算叙事见 [`docs/makeup_ledger.md`](docs/makeup_ledger.md)，测试见 `tests/test_makeup_ledger.py`。原结算/核对/影响分析入口与输出保持不变（既有 48 个测试与 `park_result.json` 逐字段一致）。

最小 CLI 流程：

```bash
# 开账（固定结算输入 + 无重叠原账单）
python3 -I /workspace/meter_allocator.py \
  --ledger-open /workspace/examples/ledger_opening.json \
  --ledger-store /tmp/park_ledger.json
# 接收一个批次（一张或多张换单，整体校验/改判/补记/落盘）
python3 -I /workspace/meter_allocator.py \
  --ledger-receive /workspace/examples/ledger_batch_1.json \
  --ledger-store /tmp/park_ledger.json \
  --ledger-postings-csv /tmp/postings.csv
# 查询某批冻结视图 / 批次清单
python3 -I /workspace/meter_allocator.py --ledger-view R1-2025-03-10 --ledger-store /tmp/park_ledger.json
python3 -I /workspace/meter_allocator.py --ledger-history --ledger-store /tmp/park_ledger.json
```

也可用等价入口 `python3 -I /workspace/makeup_ledger.py ...`。Python API 核心：

```python
ledger = MOD.open_makeup_ledger(settlement=root, original_bills=[...], store_path="ledger.json")
result = ledger.receive_batch({"batch_id": "R1", "exchanges": [...]})
ledger.view("R1")            # 该批后冻结的归属/日分摊/日差额/结算依据
MOD.MakeupLedger.load("ledger.json")
```

关键规则：

- **开账门控**：账单覆盖的每个 `(scope, day)` 都要有已核验结算格，相关格与读数段全部 settled/verified，系统金额合计非负；基线有任何无法归属 scope 的结算阻断证据即拒绝开账。
- **分摊**：行金额（非负整数分）按各日精确系统金额比例，先 floor 再按小数余量从大到小补齐余分，同余量取较早日期，全零等权。日差额 = 分摊分值 − 四舍五入到分的系统日金额。
- **裁定**：按 `effective_seq` 从开账折叠；区间内每天当时必须归属于所列旧行之一、且每个旧行至少命中一天，否则整单 `pending_verification`（待核）、无日变更。生效只替换区间内归属与分摊，旧行剩余日期的已分金额不变。
- **增量**：只重裁新单及同范围经日期重叠传递可达的较晚单；结果与“全部已收单按序号从开账重判”严格一致（`verify_incremental_matches_full_refold()`）。迟到早序单可让晚序待核单生效，也可让原已生效单转待核。
- **补记/冲回**：每条日分录 = 当前日差额 − 接收前日差额；`>0` 补记、`<0` 冲回。
- **唯一性/幂等/原子**：新行号全账本唯一；有效编号同内容重送为 no-op、改内容拒绝；结构非法整批不接收；接收、改判、分录与原子落盘整体生效；每批留存含旧行/新行/结算依据的冻结财务视图。


