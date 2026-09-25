"""离线虚构航道拓扑与航段安全规则。

核心概念
--------
* ``Segment``：一段单向/双向受控航道（虚构离线数据）。
  - 双向航段上，反向任务不得会遇；
  - 同向任务必须满足 ``same_direction_headway``（进入同一航段的最小发船间隔）；
  - ``min_offset`` 供拓扑级默认间隔使用，可按航段覆盖。
* ``RouteStep``：引航任务路由上的一步，指定经过哪个航段、以什么方向、
  进入该航段的相对偏移（分钟）与通过时长（分钟）。
* ``Topology``：航段集合 + 命名路由模板，支持冻结（离线快照语义）。

方向说明
~~~~~~~~
``direction`` 为 ``+1``（上行/正向）或 ``-1``（下行/反向）。同一航段上
两个占用方向相反即构成“会遇”，无论时间区间如何只要重叠就禁止。
"""
from __future__ import annotations

from dataclasses import dataclass, field


UP = +1
DOWN = -1


def parse_direction(text: str) -> int:
    t = text.strip().lower()
    if t in ("up", "+1", "1", "positive", "u"):
        return UP
    if t in ("down", "-1", "negative", "d"):
        return DOWN
    raise ValueError(f"无法解析航行方向: {text!r}（应为 up/down）")


def direction_label(direction: int) -> str:
    return "up" if direction == UP else "down"


@dataclass(frozen=True)
class Segment:
    """受控航段（离线虚构数据）。

    bidirectional=False 时表示航段本身只允许一个方向（当前模型按同向规则
    校验；反向进入会直接视为非法路由，由 Topology.route 校验）。
    """

    code: str
    name: str
    same_direction_headway: int = 30
    min_offset: int = 0
    bidirectional: bool = True
    # 仅作展示的虚构里程（海里），不参与排班
    nautical_miles: float = 0.0

    def __post_init__(self) -> None:
        if self.same_direction_headway < 0:
            raise ValueError(f"航段 {self.code} 的同向间隔不能为负")
        if self.min_offset < 0:
            raise ValueError(f"航段 {self.code} 的 min_offset 不能为负")


@dataclass(frozen=True)
class RouteStep:
    """路由上的一步：任务经过 segment 时的占用定义。

    entry_offset_min: 相对任务开始时间，进入该航段的偏移（分钟）。
    duration_min: 通过该航段所需的分钟数（占用时长）。
    """

    segment_code: str
    direction: int
    entry_offset_min: int
    duration_min: int

    def __post_init__(self) -> None:
        if self.direction not in (UP, DOWN):
            raise ValueError(
                f"航段 {self.segment_code} 方向非法: {self.direction}"
            )
        if self.entry_offset_min < 0:
            raise ValueError("航段进入偏移不能为负")
        if self.duration_min <= 0:
            raise ValueError("航段通过时长必须为正数")

    @property
    def exit_offset_min(self) -> int:
        return self.entry_offset_min + self.duration_min

    def entry_at(self, task_start: int) -> int:
        return task_start + self.entry_offset_min

    def exit_at(self, task_start: int) -> int:
        return task_start + self.exit_offset_min


@dataclass(frozen=True)
class Route:
    """一条命名路由（航段序列）。"""

    name: str
    steps: tuple[RouteStep, ...]

    @property
    def duration_min(self) -> int:
        return max(step.exit_offset_min for step in self.steps)


@dataclass
class Topology:
    """航道拓扑：航段字典 + 命名路由模板，可冻结。"""

    segments: dict[str, Segment] = field(default_factory=dict)
    routes: dict[str, Route] = field(default_factory=dict)
    frozen: bool = False

    # ---- 构造 ----
    def add_segment(self, segment: Segment) -> None:
        self._check_mutable()
        if segment.code in self.segments:
            raise ValueError(f"航段 {segment.code} 已存在")
        self.segments[segment.code] = segment

    def add_route(self, route: Route) -> None:
        self._check_mutable()
        for step in route.steps:
            self._validate_step(step)
        if route.name in self.routes:
            raise ValueError(f"路由 {route.name} 已存在")
        self.routes[route.name] = route

    def _validate_step(self, step: RouteStep) -> None:
        seg = self.segments.get(step.segment_code)
        if seg is None:
            raise ValueError(f"路由引用了不存在的航段: {step.segment_code}")
        if not seg.bidirectional:
            # 单向航段只允许 UP（虚构约定）
            if step.direction != UP:
                raise ValueError(
                    f"单向航段 {seg.code} 不允许 down 方向通过"
                )

    def freeze(self) -> None:
        self.frozen = True

    def _check_mutable(self) -> None:
        if self.frozen:
            raise RuntimeError("拓扑已冻结为快照，不能再修改")

    # ---- 查询 ----
    def segment(self, code: str) -> Segment:
        if code not in self.segments:
            raise KeyError(f"未知航段: {code}")
        return self.segments[code]

    def route(self, name: str) -> Route:
        if name not in self.routes:
            raise KeyError(f"未知路由: {name}")
        return self.routes[name]

    def validate_steps(self, steps: tuple[RouteStep, ...]) -> None:
        """校验任务自带航段序列（逐项校验）。"""
        if not steps:
            raise ValueError("任务至少要映射到一个航段")
        for step in steps:
            self._validate_step(step)

    def snapshot(self) -> "TopologySnapshot":
        """导出不可变快照（用于锁定计划保存拓扑/规则快照）。"""
        return TopologySnapshot(
            segments=dict(self.segments),
            routes=dict(self.routes),
        )


@dataclass(frozen=True)
class TopologySnapshot:
    """拓扑/规则的不可变快照。锁定计划后，外部拓扑变化不影响本快照。"""

    segments: dict[str, Segment]
    routes: dict[str, Route]

    def segment(self, code: str) -> Segment:
        return self.segments[code]

    def route(self, name: str) -> Route:
        return self.routes[name]

    def validate_steps(self, steps: tuple[RouteStep, ...]) -> None:
        if not steps:
            raise ValueError("任务至少要映射到一个航段")
        for step in steps:
            seg = self.segments[step.segment_code]
            if not seg.bidirectional and step.direction != UP:
                raise ValueError(f"快照中单向航段 {seg.code} 不允许 down")
