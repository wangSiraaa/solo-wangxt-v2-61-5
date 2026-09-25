"""排班领域模型：潮窗、引航员、接送艇、引航任务与转场规则。

每个引航任务必须映射到一个或多个航段（``RouteStep``），任务的航段占用
随开始时间平移进入同一排班模型，与潮窗、人员、接送艇、转场一起求解。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .topology import RouteStep
from .time_utils import to_min
from datetime import datetime


@dataclass(frozen=True)
class TideWindow:
    """潮窗（半开区间 [open, close)，绝对分钟）。任务必须整体落入某个潮窗。"""

    open_min: int
    close_min: int
    label: str = ""

    def contains(self, start_min: int, end_min: int) -> bool:
        return self.open_min <= start_min and end_min <= self.close_min

    @classmethod
    def from_datetimes(
        cls, open_dt: datetime, close_dt: datetime, label: str = ""
    ) -> "TideWindow":
        return cls(to_min(open_dt), to_min(close_dt), label)


@dataclass(frozen=True)
class Pilot:
    """引航员。grade 为等级，任务可要求最低等级。"""

    id: str
    name: str
    grade: int = 1


@dataclass(frozen=True)
class Boat:
    """接送艇。

    lead_min/tail_min：任务在接送艇资源上的占用向前后扩展
    （例如提前备艇、任务结束后还艇）。
    """

    id: str
    name: str
    lead_min: int = 0
    tail_min: int = 0


@dataclass(frozen=True)
class PilotageTask:
    """引航任务：映射到一个或多个航段。

    steps: 航段序列（每步含方向、进入偏移、通过时长）。
    tide_required: 是否必须落在潮窗内。
    min_pilot_grade: 对引航员等级的最低要求。
    boat_required: 是否需要接送艇（为 False 时不占艇资源）。
    """

    id: str
    vessel: str
    steps: tuple[RouteStep, ...]
    earliest_start_min: int
    latest_start_min: int
    tide_required: bool = True
    min_pilot_grade: int = 1
    boat_required: bool = True
    # 任务开始/结束地点，用于人员/艇的转场判断；None 表示不约束
    origin: Optional[str] = None
    destination: Optional[str] = None
    # 步长粒度（分钟），用于枚举候选开始时间
    start_granularity_min: int = 10

    @property
    def duration_min(self) -> int:
        return max(step.exit_offset_min for step in self.steps)

    def end_min(self, start_min: int) -> int:
        return start_min + self.duration_min

    @property
    def segment_codes(self) -> tuple[str, ...]:
        seen: list[str] = []
        for step in self.steps:
            if step.segment_code not in seen:
                seen.append(step.segment_code)
        return tuple(seen)

    @classmethod
    def from_route(
        cls,
        task_id: str,
        vessel: str,
        steps: tuple[RouteStep, ...],
        earliest_start: datetime,
        latest_start: datetime,
        **kwargs,
    ) -> "PilotageTask":
        return cls(
            id=task_id,
            vessel=vessel,
            steps=tuple(steps),
            earliest_start_min=to_min(earliest_start),
            latest_start_min=to_min(latest_start),
            **kwargs,
        )


@dataclass(frozen=True)
class StepOccupancy:
    """任务排定后，单航段的实际占用（绝对分钟，半开区间）。"""

    task_id: str
    segment_code: str
    direction: int
    entry_min: int
    exit_min: int


@dataclass
class RelocationTable:
    """转场表：同一资源连续两个地点之间所需的最小间隔（分钟）。

    key: ((from_location, to_location), minutes)。人员和接送艇各自持有一张表。
    缺失的地点对默认不可转场（infeasible），除非通过 ``default_min`` 给出
    宽松默认；首班任务没有前置地点，不做转场检查。
    """

    travel_min: dict[tuple[str, str], int] = field(default_factory=dict)
    default_min: Optional[int] = None

    def required_gap(self, from_location: str, to_location: str) -> Optional[int]:
        if from_location == to_location:
            return 0
        key = (from_location, to_location)
        if key in self.travel_min:
            return self.travel_min[key]
        rev = (to_location, from_location)
        if rev in self.travel_min:
            # 转场时间按对称处理（虚构离线数据常见约定）
            return self.travel_min[rev]
        return self.default_min
