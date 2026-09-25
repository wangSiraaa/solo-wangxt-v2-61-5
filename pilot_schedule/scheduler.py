"""排班求解器：航段拓扑约束与潮窗/人员/接送艇/转场进入同一个回溯模型。

解空间
======
每个任务选择：开始时间（任务窗口内、按粒度枚举、且整体落入某个潮窗）、
一名合格引航员、（需要时）一艘接送艇。

增量判定
========
按任务逐个放入，每次放入时检查：
* 与已放置任务在共享航段上的 会遇 / headway / 追越；
* 引航员的双重占用与转场；
* 接送艇的双重占用（含备艇/还艇缓冲）与转场。
不同航段上的任务互不影响，因此天然允许并行。

无解诊断
========
当完整回溯失败时，用“最早可行时间 + 最早空闲资源”的贪心放置复现阻塞，
收集每个放置失败任务的全部冲突，冲突中带具体航段、时间段和阻塞任务。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .constraints import (
    Conflict,
    NO_BOAT,
    NO_PILOT,
    PILOT_SKILL,
    TIDE,
    check_resource_schedule,
    check_segment_pair,
    check_tide,
)
from .domain import (
    Boat,
    Pilot,
    PilotageTask,
    RelocationTable,
    StepOccupancy,
    TideWindow,
)
from .topology import Topology, TopologySnapshot
from .time_utils import clock_label


@dataclass(frozen=True)
class Assignment:
    task_id: str
    start_min: int
    end_min: int
    pilot_id: str
    boat_id: Optional[str]
    occupancies: tuple[StepOccupancy, ...]

    def occupancy_on(self, segment_code: str) -> Optional[StepOccupancy]:
        for occ in self.occupancies:
            if occ.segment_code == segment_code:
                return occ
        return None


@dataclass
class Schedule:
    assignments: dict[str, Assignment]

    def get(self, task_id: str) -> Assignment:
        return self.assignments[task_id]

    def all_occupancies(self) -> list[StepOccupancy]:
        return [occ for a in self.assignments.values() for occ in a.occupancies]


@dataclass
class Infeasible:
    """无解结果：携带结构化冲突列表（具体航段/时间段/阻塞任务）。"""

    conflicts: tuple[Conflict, ...]
    message: str

    def grouped_by_task(self) -> dict[str, list[Conflict]]:
        grouped: dict[str, list[Conflict]] = {}
        for c in self.conflicts:
            key = c.task_id or "<global>"
            grouped.setdefault(key, []).append(c)
        return grouped


@dataclass
class Problem:
    topology: Topology | TopologySnapshot
    tasks: list[PilotageTask]
    tide_windows: list[TideWindow]
    pilots: list[Pilot]
    boats: list[Boat]
    pilot_relocations: RelocationTable = field(default_factory=RelocationTable)
    boat_relocations: RelocationTable = field(default_factory=RelocationTable)

    def __post_init__(self) -> None:
        for task in self.tasks:
            self.topology.validate_steps(task.steps)
        ids = [t.id for t in self.tasks]
        if len(set(ids)) != len(ids):
            raise ValueError("任务 ID 重复")


# ---------------------------------------------------------------------------
# 求解
# ---------------------------------------------------------------------------
@dataclass
class _State:
    assignments: dict[str, Assignment]
    # 资源 -> 已分配的 (任务, 开始时间)
    pilot_loads: dict[str, list[tuple[PilotageTask, int]]]
    boat_loads: dict[str, list[tuple[PilotageTask, int]]]


def solve(problem: Problem) -> Schedule | Infeasible:
    """联合求解；成功返回 Schedule，失败返回 Infeasible（带冲突明细）。"""
    tasks = _ordered_tasks(problem)
    state = _State(
        assignments={},
        pilot_loads={p.id: [] for p in problem.pilots},
        boat_loads={b.id: [] for b in problem.boats},
    )

    if _backtrack(problem, tasks, 0, state):
        return Schedule(assignments=dict(state.assignments))

    return _diagnose(problem, tasks)


def _ordered_tasks(problem: Problem) -> list[PilotageTask]:
    """约束紧的任务优先：需要潮窗、窗口窄、占用航段多的先排。"""
    def tightness(t: PilotageTask) -> tuple:
        window = t.latest_start_min - t.earliest_start_min
        return (
            0 if t.tide_required else 1,
            window,
            -len(t.steps),
            t.earliest_start_min,
            t.id,
        )

    return sorted(problem.tasks, key=tightness)


def _candidate_starts(
    problem: Problem, task: PilotageTask
) -> list[int]:
    """枚举候选开始时间：窗口内按粒度，且整体落在某潮窗内（需要潮窗时）。"""
    gran = max(1, task.start_granularity_min)
    starts: list[int] = []
    t = task.earliest_start_min
    while t <= task.latest_start_min:
        end = task.end_min(t)
        if not task.tide_required or any(
            w.contains(t, end) for w in problem.tide_windows
        ):
            starts.append(t)
        t += gran
    return starts


def _eligible_pilots(problem: Problem, task: PilotageTask) -> list[Pilot]:
    return [p for p in problem.pilots if p.grade >= task.min_pilot_grade]


def _backtrack(
    problem: Problem,
    tasks: list[PilotageTask],
    index: int,
    state: _State,
) -> bool:
    if index == len(tasks):
        return True

    task = tasks[index]
    eligible = _eligible_pilots(problem, task)
    candidate_boats = problem.boats if task.boat_required else None

    for start in _candidate_starts(problem, task):
        for pilot in eligible:
            if not _pilot_ok(problem, state, pilot, task, start):
                continue
            if candidate_boats is None:
                if _commit(problem, state, task, start, pilot, None):
                    if _backtrack(problem, tasks, index + 1, state):
                        return True
                    _rollback(state, task, pilot.id, None)
                continue
            for boat in candidate_boats:
                if not _boat_ok(problem, state, boat, task, start):
                    continue
                if _commit(problem, state, task, start, pilot, boat):
                    if _backtrack(problem, tasks, index + 1, state):
                        return True
                    _rollback(state, task, pilot.id, boat.id)
    return False


# ---- 增量可行性检查 ----
def _segment_ok(
    problem: Problem,
    state: _State,
    task: PilotageTask,
    start: int,
) -> bool:
    for step in task.steps:
        entry = step.entry_at(start)
        exit_ = step.exit_at(start)
        occ = StepOccupancy(
            task_id=task.id,
            segment_code=step.segment_code,
            direction=step.direction,
            entry_min=entry,
            exit_min=exit_,
        )
        segment = problem.topology.segment(step.segment_code)
        for placed in state.assignments.values():
            other = placed.occupancy_on(step.segment_code)
            if other is None:
                continue
            if check_segment_pair(occ, other, segment) is not None:
                return False
    return True


def _pilot_ok(
    problem: Problem,
    state: _State,
    pilot: Pilot,
    task: PilotageTask,
    start: int,
) -> bool:
    conflicts = check_resource_schedule(
        pilot.id,
        "pilot",
        state.pilot_loads[pilot.id] + [(task, start)],
        problem.pilot_relocations,
    )
    return not conflicts


def _boat_ok(
    problem: Problem,
    state: _State,
    boat: Boat,
    task: PilotageTask,
    start: int,
) -> bool:
    conflicts = check_resource_schedule(
        boat.id,
        "boat",
        state.boat_loads[boat.id] + [(task, start)],
        problem.boat_relocations,
        boat=boat,
    )
    return not conflicts


def _commit(
    problem: Problem,
    state: _State,
    task: PilotageTask,
    start: int,
    pilot: Pilot,
    boat: Optional[Boat],
) -> bool:
    # 航段检查独立于资源选择，提交前统一检查一次
    if not _segment_ok(problem, state, task, start):
        return False

    occupancies = tuple(
        StepOccupancy(
            task_id=task.id,
            segment_code=step.segment_code,
            direction=step.direction,
            entry_min=step.entry_at(start),
            exit_min=step.exit_at(start),
        )
        for step in task.steps
    )
    state.assignments[task.id] = Assignment(
        task_id=task.id,
        start_min=start,
        end_min=task.end_min(start),
        pilot_id=pilot.id,
        boat_id=boat.id if boat else None,
        occupancies=occupancies,
    )
    state.pilot_loads[pilot.id].append((task, start))
    if boat is not None:
        state.boat_loads[boat.id].append((task, start))
    return True


def _rollback(
    state: _State, task: PilotageTask, pilot_id: str, boat_id: Optional[str]
) -> None:
    state.assignments.pop(task.id, None)
    state.pilot_loads[pilot_id] = [
        (t, s) for (t, s) in state.pilot_loads[pilot_id] if t.id != task.id
    ]
    if boat_id is not None:
        state.boat_loads[boat_id] = [
            (t, s) for (t, s) in state.boat_loads[boat_id] if t.id != task.id
        ]


# ---------------------------------------------------------------------------
# 无解诊断：贪心最早放置，收集全部阻塞冲突
# ---------------------------------------------------------------------------
def _diagnose(problem: Problem, tasks: list[PilotageTask]) -> Infeasible:
    state = _State(
        assignments={},
        pilot_loads={p.id: [] for p in problem.pilots},
        boat_loads={b.id: [] for b in problem.boats},
    )
    all_conflicts: list[Conflict] = []

    for task in tasks:
        conflicts = _place_greedy(problem, state, task)
        if conflicts is None:
            continue  # 放置成功
        all_conflicts.extend(conflicts)
        # 无法放置的任务不进入状态，后续任务仍继续尝试以暴露更多冲突

    if not all_conflicts:
        all_conflicts.append(
            Conflict(
                kind=NO_PILOT,
                message="无可行解，但诊断阶段未捕获具体冲突",
            )
        )

    # 去重（同类/同任务/同阻塞者/同航段）
    seen: set = set()
    unique: list[Conflict] = []
    for c in all_conflicts:
        key = (
            c.kind,
            c.task_id,
            c.blocking_task_id,
            c.segment_code,
            c.resource_id,
            c.interval_start_min,
            c.interval_end_min,
        )
        if key not in seen:
            seen.add(key)
            unique.append(c)

    unique.sort(
        key=lambda c: (
            c.task_id or "",
            c.kind,
            c.segment_code or "",
            c.blocking_task_id or "",
        )
    )
    msg = _summarize(unique)
    return Infeasible(conflicts=tuple(unique), message=msg)


def _place_greedy(
    problem: Problem, state: _State, task: PilotageTask
) -> Optional[list[Conflict]]:
    """尝试把任务放到最早可行位置；失败时返回全部相关冲突。"""
    eligible = _eligible_pilots(problem, task)
    if not eligible:
        return [
            Conflict(
                kind=PILOT_SKILL,
                message=(
                    f"任务 {task.id} 要求引航员等级 >= {task.min_pilot_grade}，"
                    f"但无人满足"
                ),
                task_id=task.id,
            )
        ]

    best: Optional[list[Conflict]] = None
    for start in _candidate_starts(problem, task):
        conflicts: list[Conflict] = []

        tide_conflict = check_tide(task, start, problem.tide_windows)
        if tide_conflict is not None:
            conflicts.append(tide_conflict)

        # 航段冲突：对每个候选引航员都一样，与资源无关，检查一次
        for step in task.steps:
            occ = StepOccupancy(
                task_id=task.id,
                segment_code=step.segment_code,
                direction=step.direction,
                entry_min=step.entry_at(start),
                exit_min=step.exit_at(start),
            )
            segment = problem.topology.segment(step.segment_code)
            for placed in state.assignments.values():
                other = placed.occupancy_on(step.segment_code)
                if other is not None:
                    c = check_segment_pair(occ, other, segment)
                    if c is not None:
                        conflicts.append(c)

        # 资源冲突：收集“最早可用”引航员上的冲突
        pilot_conflicts, chosen_pilot = _best_resource(
            problem, state, eligible, task, start, is_pilot=True
        )
        conflicts.extend(pilot_conflicts)

        boat_conflicts: list[Conflict] = []
        chosen_boat: Optional[Boat] = None
        if task.boat_required:
            if not problem.boats:
                boat_conflicts.append(
                    Conflict(
                        kind=NO_BOAT,
                        message=f"任务 {task.id} 需要接送艇，但池中没有艇",
                        task_id=task.id,
                    )
                )
            else:
                boat_conflicts, chosen_boat = _best_resource(
                    problem, state, problem.boats, task, start, is_pilot=False
                )
        conflicts.extend(boat_conflicts)

        if not conflicts:
            assert chosen_pilot is not None
            _commit(problem, state, task, start, chosen_pilot, chosen_boat)
            return None

        # 记录冲突最少的最早时间点
        if best is None or len(conflicts) < len(best):
            best = conflicts

    # 所有候选开始时间都不可行：若无任何时间候选（潮窗完全排除），补一条
    if best is None:
        best = [
            Conflict(
                kind=TIDE,
                message=(
                    f"任务 {task.id} 的窗口 "
                    f"{clock_label(task.earliest_start_min)}~"
                    f"{clock_label(task.latest_start_min)} 内没有时间可落入潮窗"
                ),
                task_id=task.id,
                interval_start_min=task.earliest_start_min,
                interval_end_min=task.latest_start_min,
            )
        ]
    return best


def _best_resource(
    problem: Problem,
    state: _State,
    resources: list,
    task: PilotageTask,
    start: int,
    *,
    is_pilot: bool,
):
    """返回 (该开始时间上某资源的冲突列表, 无冲突时选中的资源)。

    挑选规则：无冲突即选；否则返回冲突数最少的资源的冲突，供诊断使用。
    """
    table = problem.pilot_relocations if is_pilot else problem.boat_relocations
    loads = state.pilot_loads if is_pilot else state.boat_loads
    boat = None if is_pilot else None  # 占位，下方按类型传入

    best_conflicts: Optional[list[Conflict]] = None
    best_resource = None
    for res in resources:
        res_boat = None if is_pilot else res
        conflicts = check_resource_schedule(
            res.id,
            "pilot" if is_pilot else "boat",
            loads[res.id] + [(task, start)],
            table,
            boat=res_boat,
        )
        if not conflicts:
            return [], res
        if best_conflicts is None or len(conflicts) < len(best_conflicts):
            best_conflicts = conflicts
            best_resource = res
    return best_conflicts or [], best_resource


def _summarize(conflicts: list[Conflict]) -> str:
    kinds: dict[str, int] = {}
    for c in conflicts:
        kinds[c.kind] = kinds.get(c.kind, 0) + 1
    parts = [f"{kind}×{count}" for kind, count in sorted(kinds.items())]
    return "无可行排班：" + "，".join(parts)
