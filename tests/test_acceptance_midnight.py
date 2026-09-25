"""验收测试 4：跨午夜航段占用正确保留。

23:50 开始、通过 S1 需 40 分钟的任务，占用区间应为 23:50 ~ 次日 00:30。
* 与落在午夜后、仍与该区间重叠的反向任务 -> 必须仍被判定会遇；
* 与同向、次日 00:20 进入者 -> headway 30 分钟不足（仅 30 分钟间隔到
  00:20 实为边界：23:50->00:20 = 30 满足；改用 00:10 验证不足）；
* 与同向、次日 00:20 之后进入者 -> 通过。
"""
import unittest
from datetime import datetime, timedelta

from pilot_schedule import (
    DOWN,
    Infeasible,
    PilotageTask,
    Problem,
    RouteStep,
    TideWindow,
    solve,
)
from pilot_schedule.constraints import SEGMENT_HEADON, SEGMENT_HEADWAY
from pilot_schedule.fixtures import (
    build_demo_topology,
    demo_relocations,
    inbound_steps,
    make_boats,
    make_pilots,
)
from pilot_schedule.time_utils import clock_label

DAY = datetime(2026, 9, 24)


def _kwargs():
    return dict(
        topology=build_demo_topology(),
        # 潮窗覆盖两个自然日，证明跨日由“绝对分钟”而非时钟字符串处理
        tide_windows=[
            TideWindow.from_datetimes(
                DAY.replace(hour=20),
                DAY + timedelta(days=1, hours=8),
                "跨夜潮窗",
            )
        ],
        pilots=make_pilots(4),
        boats=make_boats(3),
        pilot_relocations=demo_relocations(),
        boat_relocations=demo_relocations(),
    )


def late_inbound(task_id: str, hour: int, minute: int,
                 day: datetime = DAY) -> PilotageTask:
    return PilotageTask.from_route(
        task_id, f"夜航轮{task_id}", inbound_steps(),
        day.replace(hour=hour, minute=minute),
        day.replace(hour=hour, minute=minute),
        origin="A", destination="C",
    )


class CrossMidnightAcceptanceTest(unittest.TestCase):
    def test_segment_occupancy_carries_past_midnight(self) -> None:
        t1 = late_inbound("N1", 23, 50)
        result = solve(Problem(tasks=[t1], **_kwargs()))
        self.assertNotIsInstance(result, Infeasible)
        a = result.get("N1")
        s1 = a.occupancy_on("S1")
        # 进入 23:50，退出次日 00:30
        self.assertEqual(clock_label(s1.entry_min), "23:50")
        self.assertEqual(clock_label(s1.exit_min), "00:30")
        self.assertEqual(s1.exit_min - s1.entry_min, 40)
        # 任务整体结束在次日 01:10（S2 00:30 进入，40 分钟）
        self.assertEqual(clock_label(a.end_min), "01:10")

    def test_head_on_after_midnight_still_blocked(self) -> None:
        # N1 上行占用 S1 23:50~00:30；N2 反向（下行），次日 00:00 开始，
        # 20 分钟后进入 S1 并占用 30 分钟，即 00:20~00:50，
        # 与 N1 的 S1 占用（至 00:30）在跨午夜区间重叠 -> 会遇。
        t1 = late_inbound("N1", 23, 50)
        short_outbound = (
            RouteStep("S2", DOWN, 0, 20),
            RouteStep("S1", DOWN, 20, 30),
        )
        t2 = PilotageTask.from_route(
            "N2", "夜航轮N2", short_outbound,
            (DAY + timedelta(days=1)).replace(hour=0, minute=0),
            (DAY + timedelta(days=1)).replace(hour=0, minute=0),
            origin="C", destination="A",
        )
        result = solve(Problem(tasks=[t1, t2], **_kwargs()))
        self.assertIsInstance(result, Infeasible)
        headon = [c for c in result.conflicts if c.kind == SEGMENT_HEADON]
        self.assertTrue(headon, "跨午夜重叠的反向任务必须报会遇")
        c = headon[0]
        self.assertEqual(c.segment_code, "S1")
        self.assertEqual({c.task_id, c.blocking_task_id}, {"N1", "N2"})
        # 重叠区间跨在午夜两侧
        self.assertLessEqual(c.interval_start_min, c.interval_end_min)

    def test_same_direction_just_after_midnight_respects_headway(self) -> None:
        t1 = late_inbound("N1", 23, 50)
        # 次日 00:10 进入 S1：间隔 20 < 30 -> headway 不足
        t2 = late_inbound(
            "N2", 0, 10, day=DAY + timedelta(days=1)
        )
        result = solve(Problem(tasks=[t1, t2], **_kwargs()))
        self.assertIsInstance(result, Infeasible)
        hw = [
            c for c in result.conflicts
            if c.kind == SEGMENT_HEADWAY and c.segment_code == "S1"
        ]
        self.assertTrue(hw)
        self.assertEqual(hw[0].actual_gap_min, 20)
        self.assertEqual(hw[0].required_gap_min, 30)

    def test_same_direction_past_headway_after_midnight_passes(self) -> None:
        t1 = late_inbound("N1", 23, 50)
        # 次日 00:20 进入 S1：间隔 30 == headway，半开边界，满足
        t2 = late_inbound(
            "N2", 0, 20, day=DAY + timedelta(days=1)
        )
        result = solve(Problem(tasks=[t1, t2], **_kwargs()))
        self.assertNotIsInstance(result, Infeasible,
                                "跨午夜后满足 headway 应通过")
        self.assertEqual(clock_label(result.get("N2").start_min), "00:20")


if __name__ == "__main__":
    unittest.main()
