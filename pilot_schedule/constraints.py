"""统一约束检查。

五类约束在同一模型内被同一份逻辑检查，且都产生结构化 :class:`Conflict``，
冲突响应可直接给出：具体航段、时间段（半开区间）、阻塞任务。

约束清单
========
1. 航段会遇（SEGMENT_HEADON）：同一航段、方向相反、时间区间重叠。
2. 航段追越（SEGMENT_OVERTAKE）：同向但后进入者先退出（顺序倒挂）。
3. 同向 headway（SEGMENT_HEADWAY）：同方向进入同一航段的时间差
   必须 >= 航段规则 same_direction_headway。
4. 潮窗（TIDE）：任务（整体）必须落入至少一个潮窗。
5. 引航员（PILOT_DOUBLE_BOOKED / PILOT_RELOCATION）：同一引航员任务
   区间不得重叠；相邻任务之间必须满足转场时间。
6. 接送艇（BOAT_DOUBLE_BOOKED / BOAT_RELOCATION）：含前后备艇/还艇
   缓冲的占用区间不得重叠；相邻任务之间必须满足转场时间。
不同航段之间没有任何互斥，因此不同航段上的任务天然可以并行。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .domain import (
    Boat,
    PilotageTask,
    RelocationTable,
    StepOccupancy,
    TideWindow,
)
from .topology import Segment
from .time_utils import clock_label, intervals_overlap, iso

# 冲突类型常量
SEGMENT_HEADON = "SEGMENT_HEADON"
SEGMENT_OVERTAKE = "SEGMENT_OVERTAKE"
SEGMENT_HEADWAY = "SEGMENT_HEADWAY"
TIDE = "TIDE"
PILOT_DOUBLE_BOOKED = "PILOT_DOUBLE_BOOKED"
PILOT_RELOCATION = "PILOT_RELOCATION"
BOAT_DOUBLE_BOOKED = "BOAT_DOUBLE_BOOKED"
BOAT_RELOCATION = "BOAT_RELOCATION"
PILOT_SKILL = "PILOT_SKILL"
NO_PILOT = "NO_PILOT"
NO_BOAT = "NO_BOAT"


@dataclass(frozen=True)
class Conflict:
    """结构化冲突：kind + 具体航段 + 时间段 + 阻塞任务。"""

    kind: str
    message: str
    task_id: Optional[str] = None
    blocking_task_id: Optional[str] = None
    segment_code: Optional[str] = None
    resource_id: Optional[str] = None
    interval_start_min: Optional[int] = None
    interval_end_min: Optional[int] = None
    required_gap_min: Optional[int] = None
    actual_gap_min: Optional[int] = None

    def to_dict(self) -> dict:
        d: dict = {
            "kind": self.kind,
            "message": self.message,
            "task_id": self.task_id,
            "blocking_task_id": self.blocking_task_id,
            "segment_code": self.segment_code,
            "resource_id": self.resource_id,
            "interval_start": iso(self.interval_start_min)
            if self.interval_start_min is not None
            else None,
            "interval_end": iso(self.interval_end_min)
            if self.interval_end_min is not None
            else None,
            "required_gap_min": self.required_gap_min,
            "actual_gap_min": self.actual_gap_min,
        }
        return d


# ---------------------------------------------------------------------------
# 航段（方向 / headway / 追越）
# ---------------------------------------------------------------------------
def check_segment_pair(
    occ_a: StepOccupancy,
    occ_b: StepOccupancy,
    segment: Segment,
) -> Optional[Conflict]:
    """检查同一航段上两个任务占用是否冲突。无冲突返回 None。"""
    assert occ_a.segment_code == occ_b.segment_code == segment.code

    if not intervals_overlap(occ_a.entry_min, occ_a.exit_min,
                            occ_b.entry_min, occ_b.exit_min):
        return None

    if occ_a.direction != occ_b.direction:
        # 同段反向 + 时间重叠 => 会遇（即使资源充足也必须阻止）
        ov_start = max(occ_a.entry_min, occ_b.entry_min)
        ov_end = min(occ_a.exit_min, occ_b.exit_min)
        return Conflict(
            kind=SEGMENT_HEADON,
            message=(
                f"航段 {segment.code} 反向会遇：任务 {occ_a.task_id} 与 "
                f"{occ_b.task_id} 在 {clock_label(ov_start)}~{clock_label(ov_end)} 重叠"
            ),
            task_id=occ_a.task_id,
            blocking_task_id=occ_b.task_id,
            segment_code=segment.code,
            interval_start_min=ov_start,
            interval_end_min=ov_end,
        )

    # 同向：禁止追越（进入顺序与离开顺序必须一致）
    if occ_a.entry_min < occ_b.entry_min and occ_a.exit_min > occ_b.exit_min:
        return Conflict(
            kind=SEGMENT_OVERTAKE,
            message=(
                f"航段 {segment.code} 出现追越：{occ_b.task_id} 后进入却先于 "
                f"{occ_a.task_id} 离开"
            ),
            task_id=occ_b.task_id,
            blocking_task_id=occ_a.task_id,
            segment_code=segment.code,
            interval_start_min=occ_b.entry_min,
            interval_end_min=occ_b.exit_min,
        )

    # 同向 headway：两次进入间隔必须 >= 规则值
    gap = abs(occ_a.entry_min - occ_b.entry_min)
    if gap < segment.same_direction_headway:
        first = occ_a if occ_a.entry_min <= occ_b.entry_min else occ_b
        second = occ_b if first is occ_a else occ_a
        return Conflict(
            kind=SEGMENT_HEADWAY,
            message=(
                f"航段 {segment.code} 同向 headway 不足：{first.task_id} 与 "
                f"{second.task_id} 进入间隔 {gap} 分钟 < "
                f"{segment.same_direction_headway} 分钟"
            ),
            task_id=second.task_id,
            blocking_task_id=first.task_id,
            segment_code=segment.code,
            interval_start_min=second.entry_min,
            interval_end_min=second.exit_min,
            required_gap_min=segment.same_direction_headway,
            actual_gap_min=gap,
        )
    return None


# ---------------------------------------------------------------------------
# 潮窗
# ---------------------------------------------------------------------------
def check_tide(
    task: PilotageTask,
    start_min: int,
    tide_windows: list[TideWindow],
) -> Optional[Conflict]:
    if not task.tide_required:
        return None
    end_min = task.end_min(start_min)
    for window in tide_windows:
        if window.contains(start_min, end_min):
            return None
    label = "、".join(
        f"{clock_label(w.open_min)}~{clock_label(w.close_min)}"
        for w in tide_windows
    ) or "无潮窗"
    return Conflict(
        kind=TIDE,
        message=(
            f"任务 {task.id} 的计划时段 "
            f"{clock_label(start_min)}~{clock_label(end_min)} 不在任何潮窗内"
            f"（潮窗：{label}）"
        ),
        task_id=task.id,
        interval_start_min=start_min,
        interval_end_min=end_min,
    )


# ---------------------------------------------------------------------------
# 引航员 / 接送艇：双重占用 + 转场
# ---------------------------------------------------------------------------
def _busy_interval(
    task: PilotageTask, start_min: int, *, boat: Optional[Boat]
) -> tuple[int, int]:
    if boat is not None:
        return (
            start_min - boat.lead_min,
            task.end_min(start_min) + boat.tail_min,
        )
    return start_min, task.end_min(start_min)


def check_resource_schedule(
    resource_id: str,
    resource_kind: str,
    assignments: list[tuple[PilotageTask, int]],
    relocations: RelocationTable,
    *,
    boat: Optional[Boat] = None,
) -> list[Conflict]:
    """检查单个资源（引航员或接送艇）上的任务序列。

    assignments: 分配到该资源的 (任务, 开始时间) 列表（任意顺序）。
    """
    conflicts: list[Conflict] = []
    ordered = sorted(assignments, key=lambda item: (item[1], item[0].id))

    is_boat = resource_kind == "boat"
    booked_kind = BOAT_DOUBLE_BOOKED if is_boat else PILOT_DOUBLE_BOOKED
    reloc_kind = BOAT_RELOCATION if is_boat else PILOT_RELOCATION
    resource_label = "接送艇" if is_boat else "引航员"

    for (t1, s1), (t2, s2) in zip(ordered, ordered[1:]):
        b1s, b1e = _busy_interval(t1, s1, boat=boat)
        b2s, b2e = _busy_interval(t2, s2, boat=boat)

        if intervals_overlap(b1s, b1e, b2s, b2e):
            conflicts.append(
                Conflict(
                    kind=booked_kind,
                    message=(
                        f"{resource_label} {resource_id} 同时被任务 {t1.id} 与 "
                        f"{t2.id} 占用（"
                        f"{clock_label(max(b1s, b2s))}~{clock_label(min(b1e, b2e))}）"
                    ),
                    task_id=t2.id,
                    blocking_task_id=t1.id,
                    resource_id=resource_id,
                    interval_start_min=max(b1s, b2s),
                    interval_end_min=min(b1e, b2e),
                )
            )
            continue

        # 转场：上一任务终点 -> 下一任务起点
        if t1.destination is not None and t2.origin is not None:
            required = relocations.required_gap(t1.destination, t2.origin)
            if required is None:
                conflicts.append(
                    Conflict(
                        kind=reloc_kind,
                        message=(
                            f"{resource_label} {resource_id} 无法从 {t1.destination} "
                            f"转场到 {t2.origin}（任务 {t1.id} -> {t2.id}）"
                        ),
                        task_id=t2.id,
                        blocking_task_id=t1.id,
                        resource_id=resource_id,
                        interval_start_min=b1e,
                        interval_end_min=b2s,
                    )
                )
            else:
                actual = b2s - b1e
                if actual < required:
                    conflicts.append(
                        Conflict(
                            kind=reloc_kind,
                            message=(
                                f"{resource_label} {resource_id} 转场时间不足："
                                f"{t1.destination}->{t2.origin} 需 {required} 分钟，"
                                f"实际仅 {actual} 分钟（{t1.id} -> {t2.id}）"
                            ),
                            task_id=t2.id,
                            blocking_task_id=t1.id,
                            resource_id=resource_id,
                            interval_start_min=b1e,
                            interval_end_min=b2s,
                            required_gap_min=required,
                            actual_gap_min=actual,
                        )
                    )
    return conflicts
