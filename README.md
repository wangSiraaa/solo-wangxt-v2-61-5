# 引航排班：离线虚构航道拓扑 + 航段安全间隔联合约束

在原有的**潮窗、引航员、接送艇、转场**排班模型上，新增**离线虚构航道拓扑、
航段占用、航行方向与安全间隔规则**，并实现**锁定快照 + 显式修订**的版本管理。

纯 Python 标准库实现，无外部依赖；测试使用标准库 `unittest`。

## 目录

```
pilot_schedule/
  time_utils.py    # 绝对分钟时间模型（跨午夜只是更大的整数）
  topology.py      # 离线虚构拓扑：Segment / RouteStep / Route / Topology / 快照
  domain.py        # 潮窗、引航员、接送艇、任务（映射到一/多个航段）、转场表
  constraints.py   # 统一约束检查：航段会遇/追越/headway、潮窗、人、艇、转场
  scheduler.py     # 回溯求解 + 无解冲突诊断（具体航段/时间段/阻塞任务）
  plans.py         # 锁定计划：拓扑规则快照、显式修订、原子落盘、版本可查
  fixtures.py      # 离线虚构拓扑工厂（S1 进港 / S2 主航道 / S3 支线）
tests/             # 验收测试（5 组）+ 联合约束测试
examples/demo.py   # 端到端演示
```

## 1. 航道拓扑与安全规则（`topology.py`）

- `Segment`：受控航段。每个航段有自己的**同向安全间隔**
  `same_direction_headway`（如 S1=30、S2=20、S3=40 分钟）与方向属性。
- `RouteStep`：任务路由上的一步 = 航段 + 方向（`UP`/`DOWN`）+ 进入偏移 +
  通过时长。**每个引航任务映射到一个或多个航段**（`PilotageTask.steps`）。
- 占用规则（`constraints.check_segment_pair`）：
  1. **同段反向不得会遇**：方向相反且占用半开区间重叠 → `SEGMENT_HEADON`；
  2. **同向必须满足 headway**：两次进入同一航段的间隔
     `< same_direction_headway` → `SEGMENT_HEADWAY`（返回要求/实际间隔）；
  3. 同向禁止**追越**（后进入者先退出）→ `SEGMENT_OVERTAKE`。
- **不同航段之间没有任何互斥**，因此不同航段上的任务天然并行。

## 2. 统一排班模型（`scheduler.py`）

航段占用/方向/间隔与潮窗、人员、接送艇、转场进入**同一个回溯模型**：
每个任务选择 `(开始时间, 引航员, 接送艇)`，逐步增量检查全部约束，任一不满足
立即剪枝。

无解时（`solve()` 返回 `Infeasible`），用"最早可行时间 + 最早空闲资源"的
贪心放置复现阻塞，冲突响应中每个 `Conflict` 都带：

| 字段 | 含义 |
| --- | --- |
| `kind` | 冲突类型（`SEGMENT_HEADON`/`SEGMENT_HEADWAY`/`TIDE`/`PILOT_*`/`BOAT_*` …） |
| `segment_code` | **具体航段** |
| `interval_start_min/end_min` | **具体时间段**（半开区间，可 `to_dict()` 转 ISO） |
| `task_id` / `blocking_task_id` | 受限任务与**阻塞任务** |
| `required_gap_min` / `actual_gap_min` | headway/转场的要求值与实际值 |

时间内部统一为"自锚点起的绝对分钟数"：**跨午夜航段无需特殊处理**，
23:50 开始、40 分钟的占用自然保留为 23:50~次日00:30。

## 3. 锁定快照与显式修订（`plans.py`）

- `PlanStore.lock_plan()` 求解成功后，把**当时的**拓扑、每航段规则、潮窗、
  人员、接送艇、转场表与排班结果整体冻结为不可变版本 `v1` 落盘。
- 锁定后再改动内存拓扑（改 headway、加航段）**不影响**已锁计划。
- 任何变化必须通过 `revise(RevisionSpec(...))` **显式修订**：基于旧快照重建
  问题 → 应用增删改 → **整体重新求解**。
- **修订失败不写半成品**：无解时不创建任何版本/临时文件，锁指针与版本清单
  保持不变；`get_plan(v)` / `get_locked()` / `get_locked_version()` 始终可查
  旧版本。
- 落盘顺序为"版本临时文件 fsync 并原子改名 → 版本清单 → 锁指针"，
  中途崩溃也不会让半成品被锁定。版本号全局单调递增（基于旧版分叉也不覆盖）。

## 4. 验收与运行

```bash
PYTHONPATH=. python3 -m unittest discover -s tests -v
PYTHONPATH=. python3 examples/demo.py
```

验收项到测试的映射：

| 验收点 | 测试 |
| --- | --- |
| 人员和艇都够时反向会遇仍被阻止 | `tests/test_acceptance_headon.py` |
| 同向满足间隔可通过（不足时阻止并给明细） | `tests/test_acceptance_headway_parallel.py::HeadwayAcceptanceTest` |
| 不同航段可并行 | `...::ParallelSegmentsAcceptanceTest` |
| 跨午夜航段占用正确保留（会遇/headway 均按跨日区间判定） | `tests/test_acceptance_midnight.py` |
| 修订失败不写半成品、旧版本始终可查询 | `tests/test_acceptance_revision.py` |
| 航段约束与潮窗/人员/艇/转场同一模型联合生效 | `tests/test_joint_constraints.py` |

> 说明：航道数据（航段、里程、路由、转场时间）均为**离线虚构数据**，
> 不代表任何真实水域。
