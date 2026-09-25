"""联合模型测试：航段约束与潮窗、人员、接送艇、转场同时求解。

场景
====
1. 潮窗太窄且与任务窗口不相交 -> TIDE 冲突（即使航道空闲、资源充足）；
2. 只有一名合格引航员（等级要求）且两任务时间重叠 ->
   PILOT_DOUBLE_BOOKED，而放宽时间后同一模型即可排开；
3. 只有一艘接送艇且备艇/还艇缓冲导致占用重叠 ->
   BOAT_DOUBLE_BOOKED；
4. 同一引航员连续两任务的转场时间不足 -> PILOT_RELOCATION，
   冲突响应给出资源、时间段、所需与实际间隔；
5. 全部约束同时满足时联合求解成功。
"""
import unittest
from datetime import datetime

from pilot_schedule import (
    Boat,
    Infeasible,
    Pilot,
    PilotageTask,
    Problem,
    RelocationTable,
    RouteStep,
    TideWindow,
    UP,
    solve,
)
from pilot_schedule.constraints import (
    BOAT_DOUBLE_BOOKED,
    PILOT_DOUBLE_BOOKED,
    PILOT_RELOCATION,
    PILOT_SKILL,
    TIDE,
)
from pilot_schedule.fixtures import (
    branch_steps,
    build_demo_topology,
    inbound_steps,
    make_pilots,
)

DAY = datetime(2026, 9, 24)


class JointModelTest(unittest.TestCase):
    def _task(self, tid, h0, h1, **kw):
        return PilotageTask.from_route(
            tid, f"轮{tid}", inbound_steps(),
            DAY.replace(hour=h0), DAY.replace(hour=h1),
            origin="A", destination="C",
            start_granularity_min=10,
            **kw,
        )

    # ---- 潮窗 ----
    def test_tide_conflict_reported_with_interval(self) -> None:
        task = self._task("T1", 12, 13)
        problem = Problem(
            topology=build_demo_topology(),
            tasks=[task],
            # 潮窗只在凌晨开放，与午后任务窗口不相交
            tide_windows=[
                TideWindow.from_datetimes(
                    DAY.replace(hour=2), DAY.replace(hour=5), "凌晨潮"
                )
            ],
            pilots=make_pilots(3),
            boats=[Boat("B1", "艇1")],
        )
        result = solve(problem)
        self.assertIsInstance(result, Infeasible)
        kinds = [c.kind for c in result.conflicts]
        self.assertIn(TIDE, kinds)
        tide = next(c for c in result.conflicts if c.kind == TIDE)
        self.assertEqual(tide.task_id, "T1")
        self.assertIsNotNone(tide.interval_start_min)

    # ---- 人员等级 + 双重占用 ----
    def test_single_eligible_pilot_double_booking(self) -> None:
        t1 = self._task("T1", 8, 8, min_pilot_grade=2)
        t2 = self._task("T2", 8, 8, min_pilot_grade=2)
        pilots = [
            Pilot("P1", "初级", grade=1),
            Pilot("P2", "高级", grade=2),
        ]
        problem = Problem(
            topology=build_demo_topology(),
            tasks=[t1, t2],
            tide_windows=[
                TideWindow.from_datetimes(
                    DAY.replace(hour=6), DAY.replace(hour=14), "潮"
                )
            ],
            pilots=pilots,
            boats=[Boat("B1", "艇1"), Boat("B2", "艇2")],
        )
        result = solve(problem)
        self.assertIsInstance(result, Infeasible)
        self.assertTrue(
            any(c.kind == PILOT_DOUBLE_BOOKED for c in result.conflicts)
        )
        c = next(c for c in result.conflicts if c.kind == PILOT_DOUBLE_BOOKED)
        self.assertEqual(c.resource_id, "P2")
        self.assertEqual(c.blocking_task_id, "T1")
        self.assertIsNotNone(c.interval_start_min)

    def test_no_eligible_pilot_reports_skill(self) -> None:
        task = self._task("T1", 8, 8, min_pilot_grade=3)
        problem = Problem(
            topology=build_demo_topology(),
            tasks=[task],
            tide_windows=[
                TideWindow.from_datetimes(
                    DAY.replace(hour=6), DAY.replace(hour=14), "潮"
                )
            ],
            pilots=[Pilot("P1", "初级", 1)],
            boats=[Boat("B1", "艇1")],
        )
        result = solve(problem)
        self.assertIsInstance(result, Infeasible)
        self.assertIn(PILOT_SKILL, [c.kind for c in result.conflicts])

    # ---- 接送艇（含缓冲） ----
    def test_single_boat_lead_tail_double_booking(self) -> None:
        t1 = self._task("T1", 8, 8)
        # T2 与 T1 航段不冲突地安排在不同航段，但只有一艘艇，
        # 备艇10/还艇10 缓冲使占用重叠
        t_branch = PilotageTask.from_route(
            "TB", "支线轮", branch_steps(dur=30),
            DAY.replace(hour=8), DAY.replace(hour=8),
            origin="B", destination="D",
        )
        problem = Problem(
            topology=build_demo_topology(),
            tasks=[t1, t_branch],
            tide_windows=[
                TideWindow.from_datetimes(
                    DAY.replace(hour=6), DAY.replace(hour=14), "潮"
                )
            ],
            pilots=make_pilots(3),
            boats=[Boat("B1", "唯一艇", lead_min=10, tail_min=10)],
        )
        result = solve(problem)
        self.assertIsInstance(result, Infeasible)
        self.assertTrue(
            any(c.kind == BOAT_DOUBLE_BOOKED for c in result.conflicts)
        )

    # ---- 转场 ----
    def test_pilot_relocation_gap_reported(self) -> None:
        # 只有一名引航员。T1 走 S1，08:00~08:40，destination=C；
        # T2 走 S3（航段不相交，隔离航道因素），09:00~09:20，origin=D。
        # C->D 转场要求 60 分钟，实际只有 20 分钟 -> PILOT_RELOCATION。
        t1 = PilotageTask.from_route(
            "T1", "轮1",
            (RouteStep("S1", UP, 0, 40),),
            DAY.replace(hour=8), DAY.replace(hour=8),
            origin="A", destination="C",
        )
        t2 = PilotageTask.from_route(
            "T2", "轮2",
            (RouteStep("S3", UP, 0, 20),),
            DAY.replace(hour=9), DAY.replace(hour=9),
            origin="D", destination="B",
        )
        reloc = RelocationTable(
            travel_min={("A", "C"): 30, ("C", "A"): 30,
                        ("C", "D"): 60, ("D", "C"): 60},
        )
        problem = Problem(
            topology=build_demo_topology(),
            tasks=[t1, t2],
            tide_windows=[
                TideWindow.from_datetimes(
                    DAY.replace(hour=6), DAY.replace(hour=14), "潮"
                )
            ],
            pilots=[Pilot("P1", "独苗", 1)],
            boats=[Boat("B1", "艇1"), Boat("B2", "艇2")],
            pilot_relocations=reloc,
            boat_relocations=RelocationTable(default_min=0),
        )
        result = solve(problem)
        self.assertIsInstance(result, Infeasible)
        reloc_conflicts = [
            c for c in result.conflicts if c.kind == PILOT_RELOCATION
        ]
        self.assertTrue(reloc_conflicts)
        c = reloc_conflicts[0]
        self.assertEqual(c.resource_id, "P1")
        self.assertEqual(c.blocking_task_id, "T1")
        self.assertEqual(c.task_id, "T2")
        self.assertEqual(c.required_gap_min, 60)
        # T1 08:40 结束，T2 09:00 开始 => 实际间隔 20
        self.assertEqual(c.actual_gap_min, 20)
        self.assertIsNotNone(c.interval_start_min)

    def test_pilot_relocation_satisfied_passes(self) -> None:
        # 同一人：T1 08:00~08:40 A->C，T2 09:40 D->B；C->D 需 60 分钟，
        # 实际 60 分钟（边界满足）-> 可排
        t1 = PilotageTask.from_route(
            "T1", "轮1",
            (RouteStep("S1", UP, 0, 40),),
            DAY.replace(hour=8), DAY.replace(hour=8),
            origin="A", destination="C",
        )
        t2 = PilotageTask.from_route(
            "T2", "轮2",
            (RouteStep("S3", UP, 0, 20),),
            DAY.replace(hour=9, minute=40), DAY.replace(hour=9, minute=40),
            origin="D", destination="B",
        )
        reloc = RelocationTable(travel_min={("C", "D"): 60})
        problem = Problem(
            topology=build_demo_topology(),
            tasks=[t1, t2],
            tide_windows=[
                TideWindow.from_datetimes(
                    DAY.replace(hour=6), DAY.replace(hour=14), "潮"
                )
            ],
            pilots=[Pilot("P1", "独苗", 1)],
            boats=[Boat("B1", "艇1"), Boat("B2", "艇2")],
            pilot_relocations=reloc,
            boat_relocations=RelocationTable(default_min=0),
        )
        result = solve(problem)
        self.assertNotIsInstance(result, Infeasible, getattr(result, "message", ""))

    # ---- 联合成功 ----
    def test_all_constraints_satisfied_joint_solve(self) -> None:
        t1 = self._task("T1", 8, 8)
        # 同段同向上行，钉死 08:30 开始（S1 headway 30 恰好满足）
        t2 = PilotageTask.from_route(
            "T2", "轮2", inbound_steps(),
            DAY.replace(hour=8, minute=30), DAY.replace(hour=8, minute=30),
            origin="A", destination="C",
        )
        t3 = PilotageTask.from_route(
            "T3", "支线轮", branch_steps(dur=30),
            DAY.replace(hour=8), DAY.replace(hour=8),
            origin="B", destination="D",
        )
        reloc = RelocationTable(default_min=0)
        problem = Problem(
            topology=build_demo_topology(),
            tasks=[t1, t2, t3],
            tide_windows=[
                TideWindow.from_datetimes(
                    DAY.replace(hour=6), DAY.replace(hour=14), "潮"
                )
            ],
            pilots=make_pilots(4),
            boats=[
                Boat("B1", "艇1", 10, 10),
                Boat("B2", "艇2", 10, 10),
                Boat("B3", "艇3", 10, 10),
            ],
            pilot_relocations=reloc,
            boat_relocations=reloc,
        )
        result = solve(problem)
        self.assertNotIsInstance(result, Infeasible, getattr(result, "message", ""))
        self.assertEqual(set(result.assignments), {"T1", "T2", "T3"})


if __name__ == "__main__":
    unittest.main()
