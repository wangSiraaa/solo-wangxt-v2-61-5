"""离线虚构数据工厂：构造一张三航段航道拓扑供排班/测试使用。

拓扑示意（非真实地理数据）::

    [A 锚地] --S1 进港航道--> [B 交汇区] --S2 主航道--> [C 港区]
                                  |
                              S3 支线航道
                                  v
                               [D 危险品泊位]

* S1/S2/S3 均为双向航段，反向会遇禁止；
* S1 headway 30 分钟，S2 headway 20 分钟，S3 headway 40 分钟。
"""
from __future__ import annotations

from .domain import (
    Boat,
    Pilot,
    RelocationTable,
)
from .topology import DOWN, RouteStep, Segment, Topology, UP


def build_demo_topology() -> Topology:
    topo = Topology()
    topo.add_segment(Segment(
        code="S1", name="进港航道",
        same_direction_headway=30, bidirectional=True, nautical_miles=4.0,
    ))
    topo.add_segment(Segment(
        code="S2", name="主航道",
        same_direction_headway=20, bidirectional=True, nautical_miles=6.0,
    ))
    topo.add_segment(Segment(
        code="S3", name="支线航道",
        same_direction_headway=40, bidirectional=True, nautical_miles=2.5,
    ))
    return topo


def inbound_steps(*, s1_offset: int = 0, s1_dur: int = 40,
                  s2_offset: int = 40, s2_dur: int = 40) -> tuple[RouteStep, ...]:
    """A -> C：正向通过 S1、S2。"""
    return (
        RouteStep("S1", UP, s1_offset, s1_dur),
        RouteStep("S2", UP, s2_offset, s2_dur),
    )


def outbound_steps(*, s2_offset: int = 0, s2_dur: int = 40,
                   s1_offset: int = 40, s1_dur: int = 40) -> tuple[RouteStep, ...]:
    """C -> A：反向通过 S2、S1。"""
    return (
        RouteStep("S2", DOWN, s2_offset, s2_dur),
        RouteStep("S1", DOWN, s1_offset, s1_dur),
    )


def branch_steps(*, offset: int = 0, dur: int = 30,
                 direction: int = UP) -> tuple[RouteStep, ...]:
    """B -> D（或反向），只占用 S3，天然可与 S1/S2 并行。"""
    return (RouteStep("S3", direction, offset, dur),)


def make_pilots(n: int = 4) -> list[Pilot]:
    return [
        Pilot(id=f"P{i}", name=f"引航员{i}", grade=1 + (1 if i == 1 else 0))
        for i in range(1, n + 1)
    ]


def make_boats(n: int = 3, lead: int = 10, tail: int = 10) -> list[Boat]:
    return [
        Boat(id=f"B{i}", name=f"接送艇{i}", lead_min=lead, tail_min=tail)
        for i in range(1, n + 1)
    ]


def demo_relocations() -> RelocationTable:
    # 人员/艇在各地点之间的转场时间（分钟），其余地点对默认 30 分钟
    return RelocationTable(
        travel_min={
            ("A", "C"): 30, ("C", "A"): 30,
            ("A", "B"): 15, ("B", "A"): 15,
            ("B", "C"): 15, ("C", "B"): 15,
            ("B", "D"): 20, ("D", "B"): 20,
            ("A", "D"): 35, ("D", "A"): 35,
            ("C", "D"): 35, ("D", "C"): 35,
        },
        default_min=30,
    )
