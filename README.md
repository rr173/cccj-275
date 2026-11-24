# 园区抄表分时整数分配器

单文件、仅用 Python 标准库（含 `zoneinfo`）的可审计求解器：`meter_allocator.py`。

> 运行环境的 `/workspace` 初始没有业务输入文件。因此这里同时给出了明确的 JSON 输入契约、可运行样例和手算例。拿到真实输入后，只要转换成该 JSON 契约即可直接运行。

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

最小全可行样例：

```bash
python3 -I /workspace/meter_allocator.py \
  --input /workspace/examples/feasible_crossday_input.json \
  --output /tmp/feasible_result.json
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

## 5. 手算例

见 [`docs/hand_examples.md`](docs/hand_examples.md)。样例输出中的关键范围：

- `R002`：夏令时重复本地时刻；
- `R009`：跨计费日，金额 `15/4`；
- `R003`：完整换表凭证，旧段 200 Wh、新段 90 Wh；
- `R007`：无凭证负下降，隔离 `-50` Wh；
- `R008`：缺换表端点，隔离待核；
- `R006`：每个局部都能舍入，但整体无分配的广义 Hall 冲突。
