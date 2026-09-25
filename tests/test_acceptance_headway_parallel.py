"""验收测试 2 & 3：同向 headway 与不同航段并行。"""
import unittest
from datetime import datetime, timedelta

from pilot_schedule import (
    Infeasible,
    PilotageTask,
    Problem,
    TideWindow,
    solve,
)
from pilot_schedule.constraints import SEGMENT_HEADWAY
from pilot_schedule.fixtures import (
    branch_steps,
    build_demo_topology,
    demo_relocations,
    inbound_steps,
    make_boats,
    make_pilots,
)

DAY = datetime(2026, 9, 24)
WIDE_TIDE = [
    TideWindow.from_datetimes(
        DAY - timedelta(hours=2), DAY + timedelta(hours=26), "宽潮窗"
    )
]


class HeadwayAcceptanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.problem_kwargs = dict(
            topology=build_demo_topology(),
            tide_windows=WIDE_TIDE,
            pilots=make_pilots(4),
            boats=make_boats(3),
            pilot_relocations=demo_relocations(),
            boat_relocations=demo_relocations(),
        )

    def test_same_direction_within_headway_passes(self) -> None:
        # 同向上行，两条船进入 S1 相隔 30 分钟（S1 headway=30），
        # 进入 S2 也相隔 30 分钟（S2 headway=20），均满足。
        t1 = PilotageTask.from_route(
            "T1", "货轮一", inbound_steps(),
            DAY.replace(hour=8), DAY.replace(hour=8),
            origin="A", destination="C",
        )
        t2 = PilotageTask.from_route(
            "T2", "货轮二", inbound_steps(),
            DAY.replace(hour=8, minute=30), DAY.replace(hour=8, minute=30),
            origin="A", destination="C",
        )
        result = solve(Problem(tasks=[t1, t2], **self.problem_kwargs))
        self.assertNotIsInstance(result, Infeasible,
                                "同向且满足 headway 应可通过")
        a1 = result.get("T1")
        a2 = result.get("T2")
        self.assertEqual(a2.start_min - a1.start_min, 30)

    def test_same_direction_under_headway_blocked_with_details(self) -> None:
        # 两条上行为窗口所迫，进入 S1 只差 10 分钟 < 30 -> headway 冲突，
        # 冲突响应必须带具体航段/时间段/阻塞任务。
        t1 = PilotageTask.from_route(
            "T1", "货轮一", inbound_steps(),
            DAY.replace(hour=8), DAY.replace(hour=8),
            origin="A", destination="C",
        )
        t2 = PilotageTask.from_route(
            "T2", "货轮二", inbound_steps(),
            DAY.replace(hour=8, minute=10), DAY.replace(hour=8, minute=10),
            origin="A", destination="C",
        )
        result = solve(Problem(tasks=[t1, t2], **self.problem_kwargs))
        self.assertIsInstance(result, Infeasible)

        hw = [c for c in result.conflicts if c.kind == SEGMENT_HEADWAY]
        self.assertTrue(hw)
        seg_codes = {c.segment_code for c in hw}
        self.assertIn("S1", seg_codes)  # S1 要求 30 分钟
        c = next(c for c in hw if c.segment_code == "S1")
        self.assertEqual(c.task_id, "T2")
        self.assertEqual(c.blocking_task_id, "T1")
        self.assertEqual(c.required_gap_min, 30)
        self.assertEqual(c.actual_gap_min, 10)
        self.assertIsNotNone(c.interval_start_min)


class ParallelSegmentsAcceptanceTest(unittest.TestCase):
    def test_different_segments_run_in_parallel(self) -> None:
        # T 走 S1/S2 进港；B 只走 S3 支线。两船时间完全重合、
        # 各自用不同引航员/艇 -> 因航段不相交，必须可以并行。
        kwargs = dict(
            topology=build_demo_topology(),
            tide_windows=WIDE_TIDE,
            pilots=make_pilots(4),
            boats=make_boats(3),
            pilot_relocations=demo_relocations(),
            boat_relocations=demo_relocations(),
        )
        t_main = PilotageTask.from_route(
            "MAIN", "干线货轮", inbound_steps(),
            DAY.replace(hour=8), DAY.replace(hour=8),
            origin="A", destination="C",
        )
        t_branch = PilotageTask.from_route(
            "BRANCH", "支线驳船", branch_steps(),
            DAY.replace(hour=8), DAY.replace(hour=8),
            origin="B", destination="D",
        )
        result = solve(Problem(tasks=[t_main, t_branch], **kwargs))
        self.assertNotIsInstance(result, Infeasible,
                                "不同航段任务应可并行")
        a_main = result.get("MAIN")
        a_branch = result.get("BRANCH")
        # 时间并行
        self.assertEqual(a_main.start_min, a_branch.start_min)
        # 资源不重叠
        self.assertNotEqual(a_main.pilot_id, a_branch.pilot_id)
        self.assertNotEqual(a_main.boat_id, a_branch.boat_id)
        # 占用航段互不相交
        segs_main = {o.segment_code for o in a_main.occupancies}
        segs_branch = {o.segment_code for o in a_branch.occupancies}
        self.assertTrue(segs_main.isdisjoint(segs_branch))


if __name__ == "__main__":
    unittest.main()
