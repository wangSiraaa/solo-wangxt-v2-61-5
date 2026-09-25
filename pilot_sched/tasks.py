"""任务 / 引航员 / 接送艇 领域模型。

一个引航任务（PilotageTask）携带：
  * 航线 legs：按顺序经过的 (航段, 方向, 在该航段上的航行时长)；
    任务由此映射到一个或多个航段；
  * 潮窗 windows：允许的起始绝对时间区间（可多个，支持跨午夜）；
    任务起始时刻 = 进入首航段的时刻（受潮窗约束）；
  * 人员/艇的占用附加量（登轮接送 pickup、离轮接送 dropoff）；
  * 起止地点（boarding / disembarking 位置），用于转场时间计算。
"""

from __future__ import annotations

from dataclasses import dataclass

from .topology import Direction


@dataclass(frozen=True)
class RouteLeg:
    """任务航线中的一段：经过某航段、方向、以及在该航段上的航行分钟数。"""

    segment: str
    direction: Direction
    duration: int
    entry_offset: int = 0
    """自任务起始时刻起，进入该航段的相对分钟数（由任务自动推算，一般不手填）。"""

    def with_entry_offset(self, offset: int) -> "RouteLeg":
        return RouteLeg(
            segment=self.segment,
            direction=self.direction,
            duration=self.duration,
            entry_offset=offset,
        )


@dataclass(frozen=True)
class Pilot:
    """引航员。

    home           : 值守基地（地点名），用于首个任务前的转场
    available_from : 自该绝对分钟起可用（None 表示自基准时刻起）
    available_to   : 至该绝对分钟止可用（None 表示不限制）
    """

    id: str
    home: str | None = None
    available_from: int | None = None
    available_to: int | None = None


@dataclass(frozen=True)
class Boat:
    """接送艇。字段含义同 Pilot。"""

    id: str
    home: str | None = None
    available_from: int | None = None
    available_to: int | None = None


@dataclass(frozen=True)
class PilotageTask:
    """一项引航任务。

    start_location/end_location : 登轮/离轮地点，参与转场计算
    pickup_minutes              : 起始前接送/备航占用分钟（人员与艇提前占用）
    dropoff_minutes             : 结束后接送占用分钟
    boat_required               : 是否需要接送艇
    fixed_pilot/fixed_boat      : 强制指定资源（None 表示由排班器选择）
    """

    id: str
    legs: tuple[RouteLeg, ...]
    tide_windows: tuple[tuple[int, int], ...]
    start_location: str
    end_location: str
    pickup_minutes: int = 0
    dropoff_minutes: int = 0
    boat_required: bool = True
    fixed_pilot: str | None = None
    fixed_boat: str | None = None

    def __post_init__(self):
        if not self.legs:
            raise ValueError(f"任务 {self.id} 至少需要一个航段")
        if not self.tide_windows:
            raise ValueError(f"任务 {self.id} 至少需要一个潮窗")
        # 自动推算每个航段相对于任务起始（= 进入首航段）的进入偏移
        offset = 0
        rebuilt = []
        for leg in self.legs:
            rebuilt.append(leg.with_entry_offset(offset))
            offset += leg.duration
        object.__setattr__(self, "legs", tuple(rebuilt))

    # ---- 航段映射与占用 ----

    @property
    def segment_names(self) -> tuple[str, ...]:
        return tuple(leg.segment for leg in self.legs)

    @property
    def sailing_duration(self) -> int:
        return sum(leg.duration for leg in self.legs)

    @property
    def total_span(self) -> int:
        """从登轮接送开始到离轮接送结束的总时长。"""
        return self.pickup_minutes + self.sailing_duration + self.dropoff_minutes

    def leg_occupancy(self, start: int) -> dict[str, tuple[int, int, Direction]]:
        """给定任务起始绝对分钟，返回 {航段: (进入, 离开, 方向)}。

        ``start`` 即进入首航段的时刻；占用按航行时长逐段累加，
        可自然跨过午夜（绝对分钟 > 1440）。
        """
        result = {}
        for leg in self.legs:
            enter = start + leg.entry_offset
            result[leg.segment] = (enter, enter + leg.duration, leg.direction)
        return result

    def resource_occupancy(self, start: int) -> tuple[int, int]:
        """人员/艇占用闭开区间：起始前 pickup 即被占用，结束后 dropoff 才释放。"""
        return start - self.pickup_minutes, start + self.sailing_duration + self.dropoff_minutes

    def in_window(self, start: int) -> bool:
        return any(lo <= start <= hi for lo, hi in self.tide_windows)
