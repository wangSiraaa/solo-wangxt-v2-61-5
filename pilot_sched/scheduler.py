"""统一排班模型与求解器。

所有约束在同一次搜索中被检查，任何候选起始时刻必须同时满足：

  潮窗   ：起始时刻（进入首航段）落在任务潮窗内；
  人员   ：同一引航员占用区间不重叠；相邻任务间满足转场时间；可用时段覆盖；
  艇     ：同一艘艇占用区间不重叠；相邻任务间满足转场时间；可用时段覆盖；
  航道   ：共享航段上反向不得会遇（且保留 opposite_clearance 安全余量）；
           同向进入间隔必须 >= 该航段 headway；不同航段互不约束、可并行。

搜索采用“逐任务、按时间粒度枚举候选起始时刻 + 回溯”的方式；失败时通过
``explain`` 返回具体航段/人员/艇、时间段和阻塞任务。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .tasks import Pilot, Boat, PilotageTask
from .topology import WaterwayTopology, SafetyRules, Direction
from .timeutil import TimeKeeper, format_clock


# ---- 冲突类型 -------------------------------------------------------------

class _SearchBudgetExceeded(Exception):
    """回溯节点预算耗尽，触发 explain 兜底。"""


CONFLICT_TIDE = "TIDE_WINDOW"
CONFLICT_PILOT = "PILOT_OVERLAP"
CONFLICT_BOAT = "BOAT_OVERLAP"
CONFLICT_TRANSFER = "TRANSFER_TIME"
CONFLICT_OPPOSITE = "OPPOSITE_MEET"
CONFLICT_HEADWAY = "SAME_DIRECTION_HEADWAY"
CONFLICT_AVAILABILITY = "RESOURCE_AVAILABLE_WINDOW"
CONFLICT_NO_RESOURCE = "NO_RESOURCE"


@dataclass
class Conflict:
    """一条冲突解释：类型、涉及资源/航段、时间段、候选与阻塞任务。"""

    kind: str
    resource: str | None = None          # 航段名 / 引航员 id / 艇 id
    candidate_task: str | None = None
    blocking_task: str | None = None
    interval: tuple[int, int] | None = None       # 冲突时间段（绝对分钟）
    blocking_interval: tuple[int, int] | None = None
    detail: str = ""

    def to_dict(self, tk: TimeKeeper | None = None) -> dict:
        def f(iv):
            if iv is None:
                return None
            return {"start": iv[0], "end": iv[1],
                    "text": f"[{format_clock(iv[0])}, {format_clock(iv[1])})"}

        return {
            "kind": self.kind,
            "resource": self.resource,
            "candidate_task": self.candidate_task,
            "blocking_task": self.blocking_task,
            "interval": f(self.interval),
            "blocking_interval": f(self.blocking_interval),
            "detail": self.detail,
        }


@dataclass
class Assignment:
    """一个已排定的任务：起始时刻 + 分配的引航员/艇。"""

    task_id: str
    start: int
    pilot_id: str
    boat_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "start": self.start,
            "start_text": format_clock(self.start),
            "pilot_id": self.pilot_id,
            "boat_id": self.boat_id,
        }


@dataclass
class SchedulingResult:
    feasible: bool
    assignments: dict[str, Assignment] = field(default_factory=dict)
    conflicts: list[Conflict] = field(default_factory=list)

    @property
    def blocked(self) -> list[dict]:
        return [c.to_dict() for c in self.conflicts]


# --------------------------------------------------------------------------


class Scheduler:
    def __init__(
        self,
        topology: WaterwayTopology,
        rules: SafetyRules | None = None,
        pilots: Iterable[Pilot] = (),
        boats: Iterable[Boat] = (),
        transfer_minutes: dict[tuple[str, str], int] | None = None,
        time_step: int = 1,
    ):
        self.topology = topology
        self.rules = rules or SafetyRules()
        self.pilots = {p.id: p for p in pilots}
        self.boats = {b.id: b for b in boats}
        # 转场时间表按无向地点对访问
        self.transfer_minutes = {}
        for (a, b), minutes in (transfer_minutes or {}).items():
            self.transfer_minutes[(a, b)] = minutes
            self.transfer_minutes[(b, a)] = minutes
        self.time_step = max(1, int(time_step))

    def transfer(self, a: str, b: str) -> int | None:
        """两地点间转场分钟；未配置返回 None（视为不可达/无穷大）。"""
        if a == b:
            return 0
        return self.transfer_minutes.get((a, b))

    # ---- 单任务候选校验 ----

    def candidate_starts(self, task: PilotageTask) -> list[int]:
        """潮窗允许的候选起始时刻（按 time_step 离散）。"""
        starts = []
        for lo, hi in task.tide_windows:
            t = lo
            while t <= hi:
                starts.append(t)
                t += self.time_step
        return sorted(set(starts))

    def check_placement(
        self,
        task: PilotageTask,
        start: int,
        pilot_id: str,
        boat_id: str | None,
        assignments: dict[str, Assignment],
        tasks: dict[str, PilotageTask],
    ) -> list[Conflict]:
        """校验把 ``task`` 放在 ``start`` 是否与已有排班冲突。

        同时覆盖潮窗、人员、艇、转场、航道五类约束。返回冲突列表（空=可行）。
        """
        conflicts: list[Conflict] = []

        # 1) 潮窗
        if not task.in_window(start):
            wins = ", ".join(
                f"[{format_clock(lo)}, {format_clock(hi)}]"
                for lo, hi in task.tide_windows
            )
            conflicts.append(Conflict(
                kind=CONFLICT_TIDE,
                candidate_task=task.id,
                interval=(start, start + task.sailing_duration),
                detail=f"起始 {format_clock(start)} 不在潮窗 {wins} 内",
            ))

        # 2) 人员可用性
        pilot = self.pilots.get(pilot_id)
        if pilot is None:
            conflicts.append(Conflict(
                kind=CONFLICT_NO_RESOURCE, resource=pilot_id,
                candidate_task=task.id, detail=f"引航员 {pilot_id} 不存在",
            ))
        else:
            p_occ_lo, p_occ_hi = task.resource_occupancy(start)
            if (pilot.available_from is not None and p_occ_lo < pilot.available_from) or \
               (pilot.available_to is not None and p_occ_hi > pilot.available_to):
                conflicts.append(Conflict(
                    kind=CONFLICT_AVAILABILITY, resource=pilot_id,
                    candidate_task=task.id,
                    interval=(p_occ_lo, p_occ_hi),
                    detail=(
                        f"引航员 {pilot_id} 占用 [{format_clock(p_occ_lo)}, "
                        f"{format_clock(p_occ_hi)}) 超出可用时段"
                    ),
                ))
            # 基地 -> 首个任务地点的转场（仅当该引航员当前没有其他任务时）
            mine = [a for a in assignments.values() if a.pilot_id == pilot_id]
            if not mine and pilot.home is not None:
                need = self.transfer(pilot.home, task.start_location)
                earliest = pilot.available_from
                if need is None:
                    conflicts.append(Conflict(
                        kind=CONFLICT_TRANSFER, resource=pilot_id,
                        candidate_task=task.id, interval=(p_occ_lo, p_occ_hi),
                        detail=(
                            f"引航员 {pilot_id} 从基地 {pilot.home} 到 "
                            f"{task.start_location} 无可达转场路径"
                        ),
                    ))
                elif earliest is not None and earliest + need > p_occ_lo:
                    conflicts.append(Conflict(
                        kind=CONFLICT_TRANSFER, resource=pilot_id,
                        candidate_task=task.id, interval=(p_occ_lo, p_occ_hi),
                        detail=(
                            f"引航员 {pilot_id} 最早 {format_clock(earliest + need)} "
                            f"才能到达 {task.start_location}"
                        ),
                    ))

        # 3) 艇
        need_boat = task.boat_required or task.fixed_boat is not None
        boat = self.boats.get(boat_id) if boat_id is not None else None
        if need_boat and boat_id is None:
            conflicts.append(Conflict(
                kind=CONFLICT_NO_RESOURCE,
                candidate_task=task.id,
                detail=f"任务 {task.id} 需要接送艇但未分配",
            ))
        elif need_boat:
            if boat is None:
                conflicts.append(Conflict(
                    kind=CONFLICT_NO_RESOURCE, resource=boat_id,
                    candidate_task=task.id, detail=f"接送艇 {boat_id} 不存在",
                ))
            else:
                b_occ_lo, b_occ_hi = task.resource_occupancy(start)
                if (boat.available_from is not None and b_occ_lo < boat.available_from) or \
                   (boat.available_to is not None and b_occ_hi > boat.available_to):
                    conflicts.append(Conflict(
                        kind=CONFLICT_AVAILABILITY, resource=boat_id,
                        candidate_task=task.id,
                        interval=(b_occ_lo, b_occ_hi),
                        detail=(
                            f"艇 {boat_id} 占用 [{format_clock(b_occ_lo)}, "
                            f"{format_clock(b_occ_hi)}) 超出可用时段"
                        ),
                    ))
                mine = [a for a in assignments.values() if a.boat_id == boat_id]
                if not mine and boat.home is not None:
                    need = self.transfer(boat.home, task.start_location)
                    earliest = boat.available_from
                    if need is None:
                        conflicts.append(Conflict(
                            kind=CONFLICT_TRANSFER, resource=boat_id,
                            candidate_task=task.id, interval=(b_occ_lo, b_occ_hi),
                            detail=(
                                f"艇 {boat_id} 从基地 {boat.home} 到 "
                                f"{task.start_location} 无可达转场路径"
                            ),
                        ))
                    elif earliest is not None and earliest + need > b_occ_lo:
                        conflicts.append(Conflict(
                            kind=CONFLICT_TRANSFER, resource=boat_id,
                            candidate_task=task.id, interval=(b_occ_lo, b_occ_hi),
                            detail=(
                                f"艇 {boat_id} 最早 {format_clock(earliest + need)} "
                                f"才能到达 {task.start_location}"
                            ),
                        ))

        # 4/5) 与每个已有任务做 人员/艇/转场/航段 成对校验
        cand_legs = task.leg_occupancy(start)
        for other_id, other_a in assignments.items():
            other = tasks[other_id]
            conflicts.extend(
                self._pair_resource_conflicts(task, start, pilot_id, boat_id,
                                              need_boat, other, other_a)
            )
            conflicts.extend(
                self._pair_segment_conflicts(task, cand_legs, other,
                                             other.leg_occupancy(other_a.start))
            )
        return conflicts

    def _pair_resource_conflicts(self, task, start, pilot_id, boat_id,
                                 need_boat, other, other_a) -> list[Conflict]:
        conflicts = []
        occ_lo, occ_hi = task.resource_occupancy(start)
        o_lo, o_hi = other.resource_occupancy(other_a.start)

        # 同一引航员：占用区间不能重叠
        if pilot_id == other_a.pilot_id:
            if occ_lo < o_hi and o_lo < occ_hi:
                conflicts.append(Conflict(
                    kind=CONFLICT_PILOT, resource=pilot_id,
                    candidate_task=task.id, blocking_task=other.id,
                    interval=(max(occ_lo, o_lo), min(occ_hi, o_hi)),
                    blocking_interval=(o_lo, o_hi),
                    detail=(
                        f"引航员 {pilot_id} 任务 {task.id} 与 {other.id} 占用重叠"
                    ),
                ))
            else:
                # 转场：较早结束的任务终点 -> 较晚开始的任务起点
                # （task 为候选任务，other 为已排任务）
                if o_hi <= occ_lo:
                    # 已排任务在前：other.end -> task.start
                    first_end, later_start_loc, gap = (
                        other.end_location, task.start_location, occ_lo - o_hi)
                else:
                    # 候选任务在前：task.end -> other.start
                    first_end, later_start_loc, gap = (
                        task.end_location, other.start_location, o_lo - occ_hi)
                need = self.transfer(first_end, later_start_loc)
                if need is None or gap < (need or 0):
                    if need is None:
                        detail = (
                            f"引航员 {pilot_id} 无法从 {first_end} 转场到 "
                            f"{later_start_loc}（无路径）"
                        )
                    else:
                        detail = (
                            f"引航员 {pilot_id} 在 {first_end}→{later_start_loc} "
                            f"需转场 {need} 分钟，实际仅 {gap} 分钟"
                        )
                    conflicts.append(Conflict(
                        kind=CONFLICT_TRANSFER, resource=pilot_id,
                        candidate_task=task.id, blocking_task=other.id,
                        interval=(max(occ_lo, o_lo), min(occ_hi, o_hi))
                        if occ_lo < o_hi and o_lo < occ_hi else None,
                        blocking_interval=(o_lo, o_hi),
                        detail=detail,
                    ))

        # 同一艘艇：同样规则
        if need_boat and boat_id is not None and boat_id == other_a.boat_id:
            if occ_lo < o_hi and o_lo < occ_hi:
                conflicts.append(Conflict(
                    kind=CONFLICT_BOAT, resource=boat_id,
                    candidate_task=task.id, blocking_task=other.id,
                    interval=(max(occ_lo, o_lo), min(occ_hi, o_hi)),
                    blocking_interval=(o_lo, o_hi),
                    detail=f"艇 {boat_id} 任务 {task.id} 与 {other.id} 占用重叠",
                ))
            else:
                if o_hi <= occ_lo:
                    first_end, later_start_loc, gap = (
                        other.end_location, task.start_location, occ_lo - o_hi)
                else:
                    first_end, later_start_loc, gap = (
                        task.end_location, other.start_location, o_lo - occ_hi)
                need = self.transfer(first_end, later_start_loc)
                if need is None or gap < (need or 0):
                    if need is None:
                        detail = (
                            f"艇 {boat_id} 无法从 {first_end} 转场到 "
                            f"{later_start_loc}（无路径）"
                        )
                    else:
                        detail = (
                            f"艇 {boat_id} 在 {first_end}→{later_start_loc} "
                            f"需转场 {need} 分钟，实际仅 {gap} 分钟"
                        )
                    conflicts.append(Conflict(
                        kind=CONFLICT_TRANSFER, resource=boat_id,
                        candidate_task=task.id, blocking_task=other.id,
                        blocking_interval=(o_lo, o_hi), detail=detail,
                    ))
        return conflicts

    def _pair_segment_conflicts(self, task, cand_legs, other,
                                other_legs) -> list[Conflict]:
        """航道约束：逐航段检查会遇/headway。不同航段天然不产生冲突。"""
        conflicts = []
        for seg_name, (c_enter, c_leave, c_dir) in cand_legs.items():
            if seg_name not in other_legs:
                continue  # 不同航段 → 可并行
            o_enter, o_leave, o_dir = other_legs[seg_name]
            segment = self.topology.segment(seg_name)
            clearance = self.rules.opposite_clearance

            opposite = (
                segment.one_way
                or (self.rules.forbid_opposite and c_dir != o_dir)
            )
            if opposite:
                # 占用区间（含安全余量）不得相交：
                # 要求 c_leave + clearance <= o_enter 或 o_leave + clearance <= c_enter
                overlap_clearance = (c_enter < o_leave + clearance
                                     and o_enter < c_leave + clearance)
                if overlap_clearance:
                    raw_overlap = c_enter < o_leave and o_enter < c_leave
                    interval = self._clash_interval(
                        c_enter, c_leave, o_enter, o_leave, clearance, raw_overlap)
                    kind = CONFLICT_OPPOSITE
                    if segment.one_way and c_dir == o_dir:
                        kind = CONFLICT_HEADWAY
                    detail = (
                        f"航段 {seg_name} 上任务 {task.id}({c_dir.value}) 与 "
                        f"{other.id}({o_dir.value}) "
                        + ("会遇重叠" if raw_overlap
                           else f"未保持 {clearance} 分钟反向安全余量")
                    )
                    conflicts.append(Conflict(
                        kind=kind, resource=seg_name,
                        candidate_task=task.id, blocking_task=other.id,
                        interval=interval,
                        blocking_interval=(o_enter, o_leave),
                        detail=detail,
                    ))
            elif c_dir == o_dir:
                headway = segment.headway
                if headway is None:
                    headway = self.rules.headway_for(seg_name)
                gap = abs(c_enter - o_enter)
                if gap < headway:
                    if c_enter <= o_enter:
                        interval = (c_enter, c_enter + headway)
                    else:
                        interval = (o_enter, o_enter + headway)
                    conflicts.append(Conflict(
                        kind=CONFLICT_HEADWAY, resource=seg_name,
                        candidate_task=task.id, blocking_task=other.id,
                        interval=interval,
                        blocking_interval=(o_enter, o_leave),
                        detail=(
                            f"航段 {seg_name} 同向({c_dir.value})任务进入间隔 "
                            f"{gap} 分钟 < headway {headway} 分钟"
                        ),
                    ))
        return conflicts

    @staticmethod
    def _clash_interval(c_enter, c_leave, o_enter, o_leave,
                        clearance, raw_overlap) -> tuple[int, int]:
        """给出冲突时间段：真实重叠取交集；仅安全余量不足则取两区间之间的缝隙。"""
        if raw_overlap:
            return max(c_enter, o_enter), min(c_leave, o_leave)
        if c_leave <= o_enter:
            return c_leave, o_enter
        return o_leave, c_enter

    # ---- 资源选择 ----

    def _eligible_pilots(self, task: PilotageTask) -> list[str]:
        if task.fixed_pilot is not None:
            return [task.fixed_pilot] if task.fixed_pilot in self.pilots else []
        return list(self.pilots)

    def _eligible_boats(self, task: PilotageTask) -> list[str | None]:
        if not (task.boat_required or task.fixed_boat is not None):
            return [None]
        if task.fixed_boat is not None:
            return [task.fixed_boat] if task.fixed_boat in self.boats else []
        return list(self.boats)

    # ---- 求解 ----

    def solve(self, tasks: Iterable[PilotageTask]) -> SchedulingResult:
        task_map = {t.id: t for t in tasks}
        ordered = sorted(
            task_map.values(),
            key=lambda t: (min(w[0] for w in t.tide_windows),
                           min(w[1] - w[0] for w in t.tide_windows), t.id),
        )

        # 第一阶段：波次打包贪心。按“波次”推进，每波在互不冲突的航段上
        # 各放一个任务，并把同一航段后续任务顺移到下一个 headway 波次；
        # 资源在波次间自然轮转。这对应实际引航排班按班期放任务的方式。
        greedy = self._wave_pack(ordered)
        if greedy is not None:
            return SchedulingResult(True, assignments=greedy)

        # 第二阶段：MRV 回溯兜底，带节点预算。
        assignments: dict[str, Assignment] = {}
        node_budget = [300]
        remaining = list(ordered)

        def backtrack() -> bool:
            if not remaining:
                return True
            node_budget[0] -= 1
            if node_budget[0] < 0:
                raise _SearchBudgetExceeded
            # MRV 用廉价的“航道可行起点数”排序（不含资源检查），最受限优先。
            best_task, best_channel, best_key = None, None, None
            for task in remaining:
                channel = self._channel_starts(task, assignments, task_map)
                if not channel:
                    return False
                key = (len(channel), task.id)
                if best_key is None or key < best_key:
                    best_task, best_channel, best_key = task, channel, key
            remaining.remove(best_task)
            # 只对采样后的少量起点做昂贵的资源匹配，按“资源接续紧密度”排序
            candidates = []
            for start in self._sample(best_channel, 6):
                best_res = self._best_resource(
                    best_task, start, assignments, task_map)
                if best_res is not None:
                    p, b, release = best_res
                    candidates.append((-release, start, p, b))
            candidates.sort()
            for _, start, p, b in candidates:
                assignments[best_task.id] = Assignment(
                    best_task.id, start, p, b)
                if backtrack():
                    return True
                del assignments[best_task.id]
            remaining.append(best_task)
            return False

        try:
            solved = backtrack()
        except _SearchBudgetExceeded:
            solved = False

        if solved:
            return SchedulingResult(True, assignments=dict(assignments))

        conflicts = self.explain(ordered, assignments, task_map)
        return SchedulingResult(False, assignments=dict(assignments),
                                conflicts=conflicts)

    @staticmethod
    def _sample(choices, k):
        """从 (start,...) 列表均匀采样至多 k 个，始终保留首尾。"""
        if len(choices) <= k:
            return choices
        idx = sorted({round(i * (len(choices) - 1) / (k - 1))
                      for i in range(k)})
        return [choices[i] for i in idx]

    def _wave_pack(self, ordered) -> dict | None:
        """波次打包贪心。

        反复扫描未排任务，每轮把“当前能放得下（航道 + 资源）”的任务放到
        其最早可行起点；放不进的任务留到下一轮（其最早可行时刻会因航段
        headway / 资源释放而后移）。资源选择最空闲组合。
        """
        task_map = {t.id: t for t in ordered}
        assignments: dict[str, Assignment] = {}
        pending = list(ordered)
        # 每轮至少要有进展（排进至少一个任务），否则判定贪心失败
        while pending:
            progressed = False
            next_pending = []
            for task in pending:
                choices = self._choices(task, assignments, task_map)
                if choices:
                    start, p, b = choices[0]
                    assignments[task.id] = Assignment(task.id, start, p, b)
                    progressed = True
                else:
                    next_pending.append(task)
            pending = next_pending
            if not progressed and pending:
                return None
        return assignments

    def _choices(self, task, assignments, task_map):
        """该任务当前所有可行的 (start, pilot, boat)；每起点只取最空闲资源。"""
        choices = []
        for start in self._channel_starts(task, assignments, task_map):
            best = self._best_resource(task, start, assignments, task_map)
            if best is not None:
                pilot_id, boat_id, release = best
                choices.append((start, pilot_id, boat_id, release))
        # 优先尝试“资源刚释放即接续”的起点（release 最大且 <= start），
        # 这对应合理的班期波次，使回溯在前几个分支就命中可行解。
        choices.sort(key=lambda c: (-c[3], c[0]))
        return [(s, p, b) for s, p, b, _ in choices]

    def _channel_starts(self, task, assignments, task_map):
        """通过潮窗 + 航段占用（与人员/艇无关）的候选起点，按时间排序。"""
        out = []
        for start in self.candidate_starts(task):
            if not task.in_window(start):
                continue
            cand_legs = task.leg_occupancy(start)
            blocked = False
            for other_a in assignments.values():
                other = task_map[other_a.task_id]
                if self._pair_segment_conflicts(
                        task, cand_legs, other,
                        other.leg_occupancy(other_a.start)):
                    blocked = True
                    break
            if not blocked:
                out.append(start)
        return out

    def _best_resource(self, task, start, assignments, task_map):
        """快速返回最空闲的可行 (pilot, boat, release)，不可行返回 None。

        release = 所选引航员/艇中较晚的“上一个任务释放时刻”；
        值越大表示该起点越贴近资源刚释放的接续时刻（适合班期对齐）。
        先按资源“上一个任务结束时刻”升序生成候选（最空闲优先），
        找到第一个通过完整 check_placement 的组合即返回。
        """
        # 维护每个资源的最晚释放时刻（懒算一次）
        pilot_busy = {pid: -1 for pid in self._eligible_pilots(task)}
        boat_busy = {bid: -1 for bid in self._eligible_boats(task)}
        for a in assignments.values():
            end = task_map[a.task_id].resource_occupancy(a.start)[1]
            if a.pilot_id in pilot_busy:
                pilot_busy[a.pilot_id] = max(pilot_busy[a.pilot_id], end)
            if a.boat_id is not None and a.boat_id in boat_busy:
                boat_busy[a.boat_id] = max(boat_busy[a.boat_id], end)
        pilots = sorted(pilot_busy, key=lambda p: pilot_busy[p])
        boats = sorted(boat_busy, key=lambda b: boat_busy[b])
        for pilot_id in pilots:
            for boat_id in boats:
                cs = self.check_placement(
                    task, start, pilot_id, boat_id, assignments, task_map)
                if not cs:
                    release = max(pilot_busy[pilot_id], boat_busy[boat_id])
                    return pilot_id, boat_id, release
        return None

    # ---- 失败解释 ----

    def explain(self, ordered, assignments, task_map) -> list[Conflict]:
        """搜索失败后的根因解释。

        对每个未排入任务缓存三类信息：
          * 航道可行起点集 ``channel``（与资源无关，只看潮窗 + 航段占用）；
          * 按人员/艇缓存的“完整可行起点集” ``resource_ok``；
        然后：
          1. 若某任务在所有资源组合下都没有完整可行起点，直接报告代表冲突；
          2. 对每对任务：先看两者航道起点集是否存在航段兼容的时间对；
             若不存在 → 强制会遇/headway 冲突（资源再充足也无解）；
             若存在但所有兼容时间对都因共用资源失败 → 资源容量冲突。
        """
        assigned_ids = set(assignments)
        remaining = [t for t in ordered if t.id not in assigned_ids]
        conflicts: list[Conflict] = []
        infos: dict[str, dict] = {}

        for task in remaining:
            channel = self._channel_starts(task, assignments, task_map)
            resource_ok: dict[tuple, list[int]] = {}
            first_bad: Conflict | None = None
            if channel:
                for pilot_id in self._eligible_pilots(task) or [None]:
                    for boat_id in self._eligible_boats(task) or [None]:
                        ok = []
                        for start in channel:
                            cs = self.check_placement(
                                task, start, pilot_id, boat_id,
                                assignments, task_map)
                            if not cs:
                                ok.append(start)
                            elif first_bad is None:
                                first_bad = self._rank_conflicts(cs)[0]
                        if ok:
                            resource_ok[(pilot_id, boat_id)] = ok
            else:
                # 航道层面就无起点：取潮窗内一个候选时刻生成代表冲突
                any_start = self.candidate_starts(task)
                if any_start:
                    s = any_start[0]
                    cs = self.check_placement(
                        task, s,
                        (self._eligible_pilots(task) or [None])[0],
                        (self._eligible_boats(task) or [None])[0],
                        assignments, task_map)
                    if cs:
                        first_bad = self._rank_conflicts(cs)[0]
            infos[task.id] = {"task": task, "channel": channel,
                              "resource_ok": resource_ok}
            if not resource_ok and first_bad is not None:
                conflicts.append(first_bad)

        ids = [t.id for t in remaining]
        for i, id_a in enumerate(ids):
            if not infos[id_a]["channel"]:
                continue
            for id_b in ids[i + 1:]:
                if not infos[id_b]["channel"]:
                    continue
                forced = self._pair_forced_conflict(
                    infos[id_a], infos[id_b], assignments, task_map)
                if forced is not None:
                    conflicts.append(forced)

        if not conflicts and remaining:
            t = remaining[0]
            conflicts.append(Conflict(
                kind=CONFLICT_NO_RESOURCE, candidate_task=t.id,
                detail="没有可同时满足全部约束的排班（人员/艇/航段容量不足）",
            ))
        return self._dedupe(conflicts)

    @staticmethod
    def _rank_conflicts(cs: list[Conflict]) -> list[Conflict]:
        rank = {
            CONFLICT_OPPOSITE: 0,
            CONFLICT_HEADWAY: 1,
            CONFLICT_TIDE: 2,
            CONFLICT_PILOT: 3,
            CONFLICT_BOAT: 4,
            CONFLICT_TRANSFER: 5,
            CONFLICT_AVAILABILITY: 6,
            CONFLICT_NO_RESOURCE: 7,
        }
        return sorted(cs, key=lambda c: rank.get(c.kind, 9))

    def _pair_forced_conflict(self, info_a, info_b,
                              assignments, task_map) -> Conflict | None:
        """判定两任务之间是否存在强制冲突。

        关键：航段兼容性与资源无关，因此：
          1. 先在两者航道可行起点集上找任意一对航段兼容的 (sa, sb)；
          2. 若不存在 → 强制会遇/headway 冲突（资源再充足也无解）；
          3. 若存在，挑一对“不同人员/艇”资源验证完整 check_placement；
             若完整无冲突则返回 None（真正可共存）；
          4. 若不同资源组合也被占满，才检查共用资源并报告容量冲突。
        """
        task_a, task_b = info_a["task"], info_b["task"]
        need_a = task_a.boat_required or task_a.fixed_boat is not None
        need_b = task_b.boat_required or task_b.fixed_boat is not None
        starts_a = sorted(info_a["channel"])
        starts_b = sorted(info_b["channel"])

        # 1) 找航段兼容时间对（每对 sa 只需找到第一个兼容 sb）
        channel_rep: Conflict | None = None
        fit_pair: tuple[int, int] | None = None
        for sa in starts_a:
            legs_a = task_a.leg_occupancy(sa)
            for sb in starts_b:
                seg_cs = self._pair_segment_conflicts(
                    task_a, legs_a, task_b, task_b.leg_occupancy(sb))
                if seg_cs:
                    if channel_rep is None:
                        channel_rep = self._rank_conflicts(seg_cs)[0]
                    continue
                fit_pair = (sa, sb)
                break
            if fit_pair is not None:
                break
        if fit_pair is None:
            return channel_rep  # 时间上无论如何都会遇/违反 headway

        # 2) 存在航段兼容时间对：尝试用不同资源完整验证
        sa, sb = fit_pair
        distinct_keys_a = [(pa, ba)
                           for (pa, ba) in info_a["resource_ok"]
                           if sa in info_a["resource_ok"][(pa, ba)]]
        distinct_keys_b = [(pb, bb)
                           for (pb, bb) in info_b["resource_ok"]
                           if sb in info_b["resource_ok"][(pb, bb)]]
        for pa, ba in distinct_keys_a:
            for pb, bb in distinct_keys_b:
                if pa == pb or (need_a and need_b and ba == bb):
                    continue
                trial = dict(assignments)
                trial[task_a.id] = Assignment(
                    task_a.id, sa, pa, ba if need_a else None)
                cs = self.check_placement(
                    task_b, sb, pb, bb, trial, task_map)
                if not [c for c in cs if c.blocking_task == task_a.id]:
                    return None  # 完全兼容

        # 3) 该时间点上资源都被占满；尝试在两者的完整可行起点集中找
        #    “航段兼容 + 不同资源”的其它时间对（最多扫描少量候选）。
        for (pa, ba), list_a in info_a["resource_ok"].items():
            for (pb, bb), list_b in info_b["resource_ok"].items():
                if pa == pb or (need_a and need_b and ba == bb):
                    continue
                for saa in list_a[:8]:
                    trial = dict(assignments)
                    trial[task_a.id] = Assignment(
                        task_a.id, saa, pa, ba if need_a else None)
                    legs_a = task_a.leg_occupancy(saa)
                    for sbb in list_b[:8]:
                        if self._pair_segment_conflicts(
                                task_a, legs_a, task_b,
                                task_b.leg_occupancy(sbb)):
                            continue
                        cs = self.check_placement(
                            task_b, sbb, pb, bb, trial, task_map)
                        if not [c for c in cs
                                if c.blocking_task == task_a.id]:
                            return None
        # 资源容量确实不足：返回一个资源类代表冲突
        for (pa, ba), list_a in info_a["resource_ok"].items():
            for (pb, bb), list_b in info_b["resource_ok"].items():
                if not (pa == pb or (need_a and need_b and ba == bb)):
                    continue
                saa = list_a[0]
                trial = dict(assignments)
                trial[task_a.id] = Assignment(
                    task_a.id, saa, pa, ba if need_a else None)
                cs = self.check_placement(
                    task_b, list_b[0], pb, bb, trial, task_map)
                pair_cs = [c for c in cs
                           if c.blocking_task == task_a.id]
                if pair_cs:
                    return self._rank_conflicts(pair_cs)[0]
        return channel_rep

    @staticmethod
    def _dedupe(conflicts: list[Conflict]) -> list[Conflict]:
        seen = set()
        out = []
        for c in conflicts:
            key = (c.kind, c.resource, c.candidate_task, c.blocking_task,
                   c.interval)
            if key not in seen:
                seen.add(key)
                out.append(c)
        return out
