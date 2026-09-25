"""规模与性能测试：保证求解/解释在较大输入下仍然快速返回。

这些测试不追求具体排班数值，只验证：
  * 可行问题在秒级内解出且全部任务排入；
  * 无解问题在秒级内返回，并带有航段/资源类根因冲突（而不是空兜底）。
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pilot_sched import (
    Direction, Segment, WaterwayTopology, SafetyRules,
    RouteLeg, PilotageTask, Pilot, Boat, Scheduler,
)
from pilot_sched.scheduler import (
    CONFLICT_OPPOSITE, CONFLICT_HEADWAY, CONFLICT_TIDE,
    CONFLICT_PILOT, CONFLICT_BOAT, CONFLICT_TRANSFER,
    CONFLICT_NO_RESOURCE,
)


def make_scheduler(segments=5, crews=8, transfers=None):
    topo = WaterwayTopology.of(
        [Segment(f"S{i}") for i in range(1, segments + 1)])
    for i in range(1, segments):
        topo.connect(f"S{i}", f"S{i + 1}")
    return Scheduler(
        topo, SafetyRules(30, 10),
        pilots=[Pilot(f"P{i}") for i in range(crews)],
        boats=[Boat(f"B{i}") for i in range(crews)],
        transfer_minutes=transfers or {},
        time_step=10)


class PerformanceTests(unittest.TestCase):

    def test_dense_feasible_problem_scales(self):
        sch = make_scheduler(transfers={("A", "B"): 20})
        for n in (10, 20, 30):
            tasks = [
                PilotageTask(
                    f"Y{i:02d}",
                    (RouteLeg(f"S{(i % 5) + 1}", Direction.UP, 30),),
                    ((600, 1200),), "A", "B")
                for i in range(n)
            ]
            t0 = time.time()
            result = sch.solve(tasks)
            elapsed = time.time() - t0
            self.assertTrue(result.feasible,
                            f"{n} 任务应可行，冲突: {result.conflicts}")
            self.assertEqual(len(result.assignments), n)
            self.assertLess(elapsed, 2.0, f"{n} 任务求解过慢: {elapsed:.2f}s")

    def test_congested_segment_returns_conflicts_fast(self):
        sch = make_scheduler(segments=1, crews=8)
        tasks = [
            PilotageTask(
                f"X{i:02d}",
                (RouteLeg("S1",
                          Direction.UP if i % 2 == 0 else Direction.DOWN,
                          40),),
                ((600, 780),), "A", "B")
            for i in range(20)
        ]
        t0 = time.time()
        result = sch.solve(tasks)
        elapsed = time.time() - t0
        self.assertFalse(result.feasible)
        self.assertLess(elapsed, 3.0)
        kinds = {c.kind for c in result.conflicts}
        self.assertTrue(
            kinds & {CONFLICT_OPPOSITE, CONFLICT_HEADWAY, CONFLICT_PILOT,
                     CONFLICT_BOAT, CONFLICT_TRANSFER, CONFLICT_NO_RESOURCE},
            f"应报告容量/航段类冲突，实际: {kinds}")

    def test_many_tasks_bounded_latency(self):
        sch = make_scheduler(segments=5, crews=8)
        tasks = [
            PilotageTask(
                f"T{i:02d}",
                (RouteLeg(f"S{(i % 5) + 1}",
                          Direction.UP if i % 2 == 0 else Direction.DOWN,
                          40),),
                ((600, 960),), "A", "B")
            for i in range(30)
        ]
        t0 = time.time()
        sch.solve(tasks)
        self.assertLess(time.time() - t0, 5.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
