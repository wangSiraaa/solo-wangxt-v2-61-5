"""验收测试 1：人员和艇都够时，同段反向会遇仍被阻止。

验收点
------
* 资源完全充足（4 名引航员、3 艘艇，一人一艇），阻塞原因只能是航道；
* 两任务映射到同一航段 S1、方向相反、时间窗重叠 -> SEGMENT_HEADON；
* 冲突响应返回具体航段（segment_code）、时间段（重叠区间）、
  阻塞任务（blocking_task_id）；
* 放宽时间窗口后（同段反向但不在同一时段）即可排开。
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
    UP,
    solve,
)
from pilot_schedule.constraints import SEGMENT_HEADON
from pilot_schedule.fixtures import (
    build_demo_topology,
    demo_relocations,
    make_boats,
    make_pilots,
)
from pilot_schedule.time_utils import clock_label

DAY = datetime(2026, 9, 24)


def tide_all_day(day: datetime) -> TideWindow:
    return TideWindow.from_datetimes(
        day - timedelta(hours=2), day + timedelta(hours=26), "宽潮窗"
    )


def up_s1(task_id: str, vessel: str, h: int, m: int = 0,
          window: int = 0) -> PilotageTask:
    """只映射到 S1 的上行任务（entry 0，通过 40 分钟）。"""
    start = DAY.replace(hour=h, minute=m)
    return PilotageTask.from_route(
        task_id, vessel,
        (RouteStep("S1", UP, 0, 40),),
        start - timedelta(minutes=window),
        start + timedelta(minutes=window),
        origin="A", destination="B",
    )


def down_s1(task_id: str, vessel: str, h: int, m: int = 0,
            window: int = 0) -> PilotageTask:
    """只映射到 S1 的下行任务（反向）。"""
    start = DAY.replace(hour=h, minute=m)
    return PilotageTask.from_route(
        task_id, vessel,
        (RouteStep("S1", DOWN, 0, 40),),
        start - timedelta(minutes=window),
        start + timedelta(minutes=window),
        origin="B", destination="A",
    )


class HeadOnAcceptanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.topo = build_demo_topology()
        self.pilots = make_pilots(4)
        self.boats = make_boats(3)
        self.reloc = demo_relocations()
        self.tides = [tide_all_day(DAY)]

    def _problem(self, t1: PilotageTask, t2: PilotageTask) -> Problem:
        return Problem(
            topology=self.topo,
            tasks=[t1, t2],
            tide_windows=self.tides,
            pilots=self.pilots,
            boats=self.boats,
            pilot_relocations=self.reloc,
            boat_relocations=self.reloc,
        )

    def test_head_on_blocked_even_with_ample_resources(self) -> None:
        # 两任务都在 S1 上、方向相反、都只能 08:00 开始、各用各的人艇。
        t_up = up_s1("T-UP", "上行轮", 8)
        t_down = down_s1("T-DOWN", "下行轮", 8)

        result = solve(self._problem(t_up, t_down))
        self.assertIsInstance(result, Infeasible)

        headon = [c for c in result.conflicts if c.kind == SEGMENT_HEADON]
        self.assertTrue(headon, f"应报告反向会遇冲突，实际：{result.message}")

        c = headon[0]
        # —— 具体航段 ——
        self.assertEqual(c.segment_code, "S1")
        # —— 阻塞任务 ——
        self.assertEqual({c.task_id, c.blocking_task_id},
                         {"T-UP", "T-DOWN"})
        # —— 时间段（非空重叠区间，且就是 08:00~08:40）——
        self.assertEqual(clock_label(c.interval_start_min), "08:00")
        self.assertEqual(clock_label(c.interval_end_min), "08:40")
        self.assertIn("S1", c.message)

        # 资源侧不应有任何冲突（人和艇都够）
        self.assertFalse(
            [c for c in result.conflicts if "PILOT" in c.kind or "BOAT" in c.kind],
            "资源充足时不应出现人员/艇冲突",
        )

        # 结构化冲突响应可直接序列化
        d = c.to_dict()
        self.assertEqual(d["kind"], SEGMENT_HEADON)
        self.assertEqual(d["segment_code"], "S1")
        self.assertIsNotNone(d["interval_start"])
        self.assertEqual(d["blocking_task_id"], c.blocking_task_id)

    def test_head_on_avoidable_when_windows_separated(self) -> None:
        # 同段反向，但窗口相隔 50 分钟（占用区间不重叠）-> 可排开
        t_up = up_s1("T-UP", "上行轮", 8)
        t_down = down_s1("T-DOWN", "下行轮", 8, 50)
        result = solve(self._problem(t_up, t_down))
        self.assertNotIsInstance(result, Infeasible, "反向但错峰应可通过")

    def test_head_on_still_blocked_when_windows_forced_to_overlap(self) -> None:
        # 即使给 10 分钟窗口浮动，也找不到任何不错开 40 分钟占用的排法
        # （07:50~08:10 与反向 07:50~08:10 的候选区间必然重叠）
        t_up = up_s1("T-UP", "上行轮", 8, window=10)
        t_down = down_s1("T-DOWN", "下行轮", 8, window=10)
        result = solve(self._problem(t_up, t_down))
        self.assertIsInstance(result, Infeasible)
        self.assertTrue(
            any(c.kind == SEGMENT_HEADON for c in result.conflicts)
        )


if __name__ == "__main__":
    unittest.main()
