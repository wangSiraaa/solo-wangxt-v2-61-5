# 离线引航排班：虚构航道拓扑 + 统一约束排班模型

纯 Python 标准库实现（无第三方依赖），把**航道拓扑/占用**与**潮窗、人员、接送艇、
转场**放进同一个排班模型，并对锁定计划保存**拓扑/规则快照**，后续变更只能通过
显式修订生成新版本。

## 1. 航道模型（离线虚构数据）

* `WaterwayTopology`：命名航段 `Segment` 集合 + 可选邻接关系（校验航线连续性）。
* 每个引航任务 `PilotageTask` 通过其航线 `RouteLeg(segment, direction, duration)`
  **映射到一个或多个航段**；多航段任务按顺序累加，自动推算每个航段的进入偏移。
* 时间使用“基准日午夜起的绝对分钟数”，因此**跨午夜占用天然正确**
  （23:30 进入、航行 90 分钟 → 次日 01:00 离开 = 1500，不做 mod 1440）。

## 2. 安全规则（`SafetyRules`）

在**同一航段**上，逐航段比较两个任务的占用区间 `[进入, 离开)`：

* **反向会遇禁止**：方向相反（或航段 `one_way=True`）时，占用区间不得相交，
  且两个区间之间必须保留 `opposite_clearance`（默认 10 分钟）安全余量；
* **同向 headway**：同向航行的两任务，进入时刻间隔必须 ≥ headway。
  全局默认 `same_direction_headway=30`，可用航段级 `segment.headway` 或
  `SafetyRules.segment_headways` 覆盖；
* **不同航段不互斥**：任务在不同航段上可以完全并行。

## 3. 统一排班模型（`Scheduler`）

`check_placement / solve` 在同一次判定/搜索中合取五类约束：

| 维度 | 规则 |
|------|------|
| 潮窗 | 起始时刻（进入首航段时刻）必须落在任务的某个潮窗内，潮窗可跨午夜 |
| 人员 | 同一引航员占用区间不重叠；相邻任务间满足转场时间；可用时段覆盖；基地可达 |
| 接送艇 | 同艇占用区间不重叠、转场时间、可用时段（任务可声明不需要艇） |
| 转场 | 上一任务结束地点 → 下一任务开始地点的路程时间（无向地点对表，未配置=不可达） |
| 航道 | 反向会遇禁止 + 安全余量、同向 headway、不同航段并行 |

搜索采用“波次打包贪心 → MRV 回溯（节点预算保护）”两阶段；失败时由 `explain`
返回**根因冲突**。

### 冲突响应

`Conflict` 精确给出：

* `kind`：`OPPOSITE_MEET` / `SAME_DIRECTION_HEADWAY` / `TIDE_WINDOW` /
  `PILOT_OVERLAP` / `BOAT_OVERLAP` / `TRANSFER_TIME` / `RESOURCE_AVAILABLE_WINDOW`
  / `NO_RESOURCE`；
* `resource`：**具体航段名**或引航员/艇 id；
* `candidate_task` / `blocking_task`：候选任务与**阻塞任务**；
* `interval` / `blocking_interval`：**具体冲突时间段**（绝对分钟 + `[HH:MM)` 文本）。

`Scheduler.check_placement(task, start, pilot, boat, plan, tasks)` 也可对一个
具体的拟排班方案直接返回上述冲突列表。

## 4. 锁定、快照与显式修订（`PlanStore`）

* `store.lock(plan_id, tasks, scheduler)`：求解并把当时的**拓扑、安全规则、
  转场表、人员/艇、任务、排班结果**整体 JSON 快照落盘为 `v1.json`。
* 锁定后内存中的拓扑/规则对象再怎么修改，都不影响已锁定版本
  （`LockedPlan.snapshot_topology() / snapshot_rules()` 从快照重建）。
* `store.revise(plan_id, ...)`：唯一的变更入口。可改任务、拓扑、规则、人员/艇、
  转场表；修订后重新求解，**可行才写新版本 v2/v3/…**，并记录
  `PlanRevision(based_on_version, changes, note)`。
* **事务性**：所有写入先写临时文件再 `os.replace` 原子替换；修订在任何一步失败
  （无可行解、非法变更、写盘错误）都不会产生新版本文件——
  **不写半成品，旧版本始终可查询**（`get(id, version)` / `versions` / `get_all`）。

## 5. 验收覆盖

`tests/test_acceptance.py` 对应需求中的五项验收：

| 用例 | 验收点 |
|------|--------|
| `test_A1_*` | **人员和艇都够时，同段反向会遇仍被阻止**；冲突返回具体航段/时间段/阻塞任务；安全余量不足也阻止 |
| `test_A2_*` | **同向满足 headway 可通过**（间隔 30），不满足（<60）被阻止 |
| `test_A3_*` | **不同航段可并行**（即使反向、人员艇充足、潮窗重叠，同时刻排入） |
| `test_A4_*` | **跨午夜航段占用正确保留**（23:30→次日 01:00），落盘快照仍为绝对分钟 |
| `test_A5_*` | **修订失败不写半成品**（磁盘只有 v1、无临时文件）、**旧版本始终可查询**、拓扑/规则变化只经显式修订生效 |
| `test_A6_*` | 潮窗、人员、艇、转场在同一模型中同时生效 |

另含：

* `tests/test_units.py`：多航段任务映射与分段占用、航段级 headway 覆盖、
  单航段水道、跨午夜潮窗、快照往返、锁定失败不落盘；
* `tests/test_scale.py`：10/20/30 任务规模下可行问题秒级解出、
  无解问题秒级返回根因冲突。

运行：

```bash
python3 -m unittest discover -s tests -v
```

## 6. 最小示例

```python
from pilot_sched import *

topo = WaterwayTopology.of([Segment("S1"), Segment("S2")])
topo.connect("S1", "S2")
sch = Scheduler(
    topology=topo,
    rules=SafetyRules(same_direction_headway=30, opposite_clearance=10),
    pilots=[Pilot("P1"), Pilot("P2")],
    boats=[Boat("B1"), Boat("B2")],
    transfer_minutes={("ANCH", "BERTH"): 20},
    time_step=10,
)
up   = PilotageTask("UP", (RouteLeg("S1", Direction.UP, 60),),
                    ((720, 780),), "ANCH", "BERTH")
down = PilotageTask("DN", (RouteLeg("S1", Direction.DOWN, 60),),
                    ((720, 780),), "ANCH", "BERTH")
result = sch.solve([up, down])
if not result.feasible:
    for c in result.blocked:   # OPPOSITE_MEET @ S1，含冲突时间段与阻塞任务
        print(c)
```
