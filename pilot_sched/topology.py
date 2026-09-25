"""离线虚构航道拓扑与安全间隔规则。

拓扑是一个带方向属性的航段（segment）集合。引航任务通过其航线（按顺序经过的
航段 + 航行方向）映射到一个或多个航段；航段占用按“任务进入该航段”到“离开该
航段”之间的闭开区间计算。

安全规则：
  * 同一航段上方向互斥的两个任务不得在任意时刻同时占用（会遇禁止）；
    当方向相反时，两者的占用区间之间还必须留足 ``opposite_clearance`` 安全余量。
  * 同一航段上同向航行的任务，进入时刻必须满足 ``headway`` 间隔。
  * 不同航段之间没有互斥——任务可以完全并行。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum


class Direction(Enum):
    """航行方向（虚构航道的两个约定方向）。

    不继承 str：避免 str-Enum 混用导致 ``is``/``==`` 语义不一致；
    需要字符串时显式使用 ``.value``。
    """

    UP = "UP"     # 上行
    DOWN = "DOWN"  # 下行

    @property
    def opposite(self) -> "Direction":
        return Direction.DOWN if self == Direction.UP else Direction.UP


@dataclass(frozen=True)
class Segment:
    """航道中的一个航段。

    name        : 航段标识（如 ``"S1"``）
    length_nm   : 长度（海里），仅用于展示/校核
    one_way     : 为 True 时任何两个任务都不得同时占用（例如单航段水道），
                  等价于两方向互斥
    headway     : 可选，覆盖规则中该航段的同向最小间隔（分钟）
    """

    name: str
    length_nm: float = 1.0
    one_way: bool = False
    headway: int | None = None


@dataclass(frozen=True)
class SafetyRules:
    """全航道安全间隔参数，可被单个航段覆盖。

    same_direction_headway : 同向任务进入同一航段的最小时间间隔（分钟）
    opposite_clearance     : 反向任务占用区间之间的最小安全余量（分钟）
    forbid_opposite        : False 时允许反向共线（默认禁止会遇）
    """

    same_direction_headway: int = 30
    opposite_clearance: int = 10
    forbid_opposite: bool = True
    segment_headways: dict[str, int] = field(default_factory=dict)

    def headway_for(self, segment: str) -> int:
        if segment in self.segment_headways:
            return self.segment_headways[segment]
        return self.same_direction_headway

    def with_segment_headway(self, segment: str, minutes: int) -> "SafetyRules":
        new_map = dict(self.segment_headways)
        new_map[segment] = minutes
        return replace(self, segment_headways=new_map)


@dataclass
class WaterwayTopology:
    """离线虚构航道拓扑：航段集合 + 相邻关系（可选，用于航线合法性提示）。

    拓扑属于离线数据：排班时快照进锁定计划，之后拓扑变化不会影响已锁定版本。
    """

    segments: dict[str, Segment] = field(default_factory=dict)
    # 邻接表：航段 -> 与其首尾相接的航段集合；仅用于校验任务航线连续性
    adjacency: dict[str, frozenset[str]] = field(default_factory=dict)

    @classmethod
    def of(cls, segments, adjacency=None) -> "WaterwayTopology":
        """便捷构造：传入 Segment 可迭代对象。"""
        seg_map = {s.name: s for s in segments}
        adj = {}
        for a, neighbors in (adjacency or {}).items():
            adj[a] = frozenset(neighbors)
        return cls(segments=seg_map, adjacency=adj)

    def add_segment(self, segment: Segment) -> None:
        if segment.name in self.segments:
            raise ValueError(f"航段已存在: {segment.name}")
        self.segments[segment.name] = segment

    def connect(self, a: str, b: str) -> None:
        """声明两个航段相邻（无向）。"""
        self._require(a)
        self._require(b)
        self.adjacency[a] = self.adjacency.get(a, frozenset()) | {b}
        self.adjacency[b] = self.adjacency.get(b, frozenset()) | {a}

    def has_segment(self, name: str) -> bool:
        return name in self.segments

    def segment(self, name: str) -> Segment:
        self._require(name)
        return self.segments[name]

    def is_continuous(self, segment_names) -> bool:
        """检查航线经过的航段序列是否相邻连续（单航段视为合法）。"""
        names = list(segment_names)
        if len(names) <= 1:
            return all(n in self.segments for n in names)
        for n in names:
            self._require(n)
        for prev, nxt in zip(names, names[1:]):
            if nxt not in self.adjacency.get(prev, frozenset()):
                return False
        return True

    def _require(self, name: str) -> None:
        if name not in self.segments:
            raise KeyError(f"拓扑中不存在航段: {name}")

    def to_snapshot(self) -> dict:
        """可 JSON 序列化的拓扑快照。"""
        return {
            "segments": [
                {
                    "name": s.name,
                    "length_nm": s.length_nm,
                    "one_way": s.one_way,
                    "headway": s.headway,
                }
                for s in self.segments.values()
            ],
            "adjacency": {a: sorted(ns) for a, ns in self.adjacency.items()},
        }

    @classmethod
    def from_snapshot(cls, data: dict) -> "WaterwayTopology":
        segments = [Segment(**s) for s in data["segments"]]
        adjacency = {a: frozenset(ns) for a, ns in data.get("adjacency", {}).items()}
        return cls(segments={s.name: s for s in segments}, adjacency=adjacency)
