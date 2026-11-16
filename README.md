# 离线设备使用区间加工器 (offline device usage-interval processor)

把**租约、门禁会话端点、遥测活动段、两类采集覆盖清单**加工成"按设备查询、
覆盖整个窗口、互不重叠"的使用区间事实。区分两种本质不同的情况：

* **确实未使用 (idle)** —— 没有任何正向证据，且门禁与遥测两类采集都完整；
* **只是采集没到 (unknown)** —— 覆盖清单之外，不能用"没收到"反推"没用过"。

离线、固定一包输入、有限 UTC 毫秒窗口；**不做实时会话增量维护，不计费用**。

## 语义规则（实现严格遵循）

| 主题 | 规则 |
|---|---|
| 租约 | `[start,end)`，暂停段从可用租约中扣除（multirange 相减） |
| 门禁配对 | 以 `(device,session_id)` 关联；同会话至多一条 enter / exit；**完整配对且 enter<exit** 才是完整门禁段。仅 enter → 可能范围 `[enter,窗口末)`；仅 exit → `[窗口初,exit)`；未知端点绝不虚构。端点可在窗口外，计算时裁到窗口 |
| 重送/冲突 | 同身份同内容重送折叠；`(session,kind)` 同刻不同内容 → 业务 conflict |
| 遥测 | 直接给 `[lo,hi)`；重叠活动取**覆盖并集**，持续时间不相加；同 id 不同范围 → conflict |
| 覆盖清单 | 只允许推出**该类**记录的缺席；清单之外出现的正向证据仍然有效 |
| conflict 优先 | ① 同时有两个可用租约；② 有完整门禁/活动证据但无可用租约（含暂停段）；③ 两段完整门禁互相重叠；④ 端点内容/活动同 id 冲突 |
| 状态判定 | conflict 优先 → 有未配对门禁可能范围 = unknown → 唯一可用租约且同时有完整门禁和活动证据 = confirmed → 无任何正向证据且两类采集都完整 = idle → 其余 unknown |
| 暂停段 | 暂停期内的正向证据**保留**，该段判 conflict，不允许过滤证据假造 idle |
| 合并 | 相邻段即使业务状态相同，也只有在**所有证据身份、租约、覆盖状态都相同**时才合并（gap-and-islands） |

## 架构

```
model.py       输入模型 + 结构校验（坏端点/坏范围 → StructuralError，整包失败）
reference.py   纯 Python 语义预言机 + 逐毫秒离散小域核对（不依赖数据库）
sql/schema.sql 暂存表、派生表、范围并集聚合、ui_build_package/ui_build_device
db.py          PostgreSQL 暂存/构建/读取/解释；设备用服务端游标流式处理
engine.py      门面：输入检查、构建、按设备/窗口读取、区间解释
cli.py         命令行接口
```

关键工程约束：

* **重活全在 PostgreSQL 内**（int8range / int8multirange、GiST、窗口函数、
  gap-and-islands）。Python 只流式取设备 id，**不把全设备全集装进内存**。
* 一个会话的多条端点**跨输入批次关联**后再判定；输入哈希对批次顺序不敏感，
  读取顺序不同不会改变结果。
* 发布结果用 `input_hash` **绑定本包输入**：同 package_id 换输入被拒绝；
  新包用新 id 重建，旧版仍可读。
* 结构问题（坏端点、坏覆盖范围等）使**本包失败且不落任何事实**；
  业务 unknown/conflict 是正常保留的有效事实。

## 环境准备（本地用户态 PostgreSQL）

仓库提供脚本在无 root 环境下下载 PostgreSQL deb 并解包到 `$HOME/pg`：

```bash
scripts/setup_local_pg.sh        # 初始化并启动 :55432，建库 usage_dev
```

## 运行

```bash
scripts/cli.sh validate --window 0 100 --package-id demo examples/*.json
scripts/cli.sh build    demo --window 0 100 examples/batch1_leases_telemetry.json \
        examples/batch2_door_exit_first.json examples/batch3_door_enter_later.json
scripts/cli.sh read     demo --device dev1
scripts/cli.sh read     demo --device dev1 --window 45 70     # 子窗口裁剪
scripts/cli.sh evidence demo --device dev1                    # 读回配对证据
scripts/cli.sh status   demo
scripts/cli.sh devices  demo
```

库内调用：

```python
from usage_intervals import Processor, load_package
pkg = load_package("pack1", {"lo": 0, "hi": 100}, batches)
with Processor(dsn="host=/tmp port=55432 user=postgres dbname=usage_dev") as p:
    p.install()
    p.process(pkg)
    p.interpret("pack1", "dev1")                 # 区间解释接口
    for dev in p.store.stream_device_ids("pack1"):   # 服务端游标流式
        ...
```

## 本地钟面时间清洗（离线，无需数据库）

老门禁网关只存当地钟面标签、不写 UTC 偏移。`usage_intervals.clock_clean`
在**不新增使用状态算法、不改动既有区间结果**的前提下，把原始标签清洗成现有
`endpoints` 输入格式：

```bash
scripts/clock_clean.sh import manifest.json -o out/      # 校验+联合消歧+落盘
scripts/clock_clean.sh explain manifest.json -r rec-id   # 解释单条记录
```

清单是一个 JSON：`streams`（流名 → IANA 时区）+ `files`（相对清单目录的
JSONL）。JSONL 每行有公共字段 `record_id/stream/seq/type`；`type=door` 带
`device/session_id/kind/content` 且恰有一个时间字段（严格无偏移的 `local`
标签或整数 `utc_ms`）；`type=anchor` 是只带 `utc_ms` 的时钟检查点，参与流内
先后约束但不产生端点。

处理要点（与正式实现一一对应，见 `usage_intervals/clock_clean/`）：

* **候选生成**：普通标签 1 个 UTC 候选，秋季回拨最多 2 个；春季跳时中不
  存在的标签 0 候选，**整次导入失败**，不自动挪到下一小时。每个候选都要能
  转回同一当地标签（不依赖 fold 想当然），不假设偏移变化都是 1 小时
  （`Australia/Lord_Howe` 只有半小时回拨）。
* **联合消歧**：跨全部文件同时考虑——同流按 `seq` 的 UTC 非递减（anchor
  同样参与，允许同刻相等）与同 `(device,session_id)` 的 enter **严格**早于
  exit（可跨流）。问题归约为 2-SAT（Tarjan SCC），线性时间给出每条记录在
  **全部可行解释**中真正可能取到的 UTC 集合；不枚举解释组合，120 个相互独立
  的双候选会话也是即时完成。无可行解释时整次失败并给出涉及的记录与关系，
  不允许删行换可行。
* **会话级导出/隔离**：两端会话只有两端都确定才导出；一端有歧义则两端一起
  进隔离报告（确定的一端标记 `held_back_partner_ambiguous`，不制造假的未配对
  会话）；原始资料只有一端且该端确定时照常导出；anchor 永不导出。
* **交付物**：`endpoints_batch.json`（仅含现有 endpoints 字段，可直接交给
  `load_package`）、`report.json`（原始标签/UTC、时区候选、联合约束后剩余
  候选、决定与原因、流内与跨流关系来源）、`sources.json`（每个导出端点回到
  原记录及全部重送物理位置）。输出目录已存在则拒绝；先完整校验分析再原子
  落盘，失败不留下可被当成成功结果的目录。

库内调用：

```python
from usage_intervals.clock_clean import clean_manifest, run_import, explain_record
result = clean_manifest("manifest.json")          # 纯内存，无数据库
```

独立核对器 `tests/clock_clean_bruteforce.py` 对小资料穷举全部候选组合
（不 import 正式求解器），在固定夹具与 60 个随机小样本上逐条比对可行集合、
隔离决定和解释实例；测试与小型夹具见 `tests/run_clock_tests.py`、
`tests/fixtures/`（各目录 `EXPECTATIONS.md` 写明人工 UTC 预期）。

## 测试与核对

```bash
python3 -I tests/run_tests.py          # 既有 usage_intervals 全链路（含 PostgreSQL）
python3 -I tests/run_clock_tests.py    # 钟面清洗：夹具+穷举交叉核对+随机器+规模+PG 接入
```

每个用例都：① 手工列出期望的完整分割；② 与纯 Python 参考实现逐段比对；
③ 在离散小域上**逐毫秒**核对状态、证据、原因；④ 真正从数据库读回区间和
配对证据（不只比总时长）。覆盖：同刻边界、双租约、门禁互相重叠、跨文件配对、
重复活动折叠、尾部结构错误、覆盖缺口、窗口外裁剪、版本绑定。

主演练窗口 `[0,100)`（L1=`[0,50)`、L2=`[50,100)`、门禁 s=`[40,70)`、
活动 a=`[45,65)`、两类覆盖完整）产出：

```
[  0, 40) idle       [ 40, 45) unknown    [ 45, 50) confirmed(L1)
[ 50, 65) confirmed(L2)   [ 65, 70) unknown   [ 70,100) idle
```

跨交接门禁不会被整段归给 L1。L2 暂停 `[55,60)` → 该段 **conflict**（证据保留）；
撤掉遥测覆盖 `[70,80)` → 该段无活动证据也必须 **unknown**；加入未配对
enter@20 后，其可能范围内不再声称 confirmed，conflict 仍优先。
