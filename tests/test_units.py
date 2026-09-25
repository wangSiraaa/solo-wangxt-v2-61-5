"""补充单元测试：多航段任务映射、航段级 headway、规则快照往返、单航段水道。"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pilot_sched import (
    Direction, Segment, WaterwayTopology, SafetyRules,
    RouteLeg, PilotageTask, Pilot, Boat, Scheduler,
    PlanStore, RevisionError,
)
from pilot_sched.scheduler import (
    CONFLICT_OPPOSITE, CONFLICT_HEADWAY, CONFLICT_TIDE,
)

D = Direction


class MultiLegTests(unittest.TestCase):

    def setUp(self):
        self.topo = WaterwayTopology.of([
            Segment("S1", 5.0), Segment("S2", 4.0), Segment("S3", 3.0)])
        self.topo.connect("S1", "S2")
        self.topo.connect("S2", "S3")
        self.sch = Scheduler(
            self.topo, SafetyRules(30, 10),
            pilots=[Pilot("P1"), Pilot("P2")],
            boats=[Boat("B1"), Boat("B2")],
            time_step=5)

    def test_task_maps_to_multiple_segments_with_offset_occupancy(self):
        t = PilotageTask(
            id="M1",
            legs=(
                RouteLeg("S1", D.UP, 20),
                RouteLeg("S2", D.UP, 30),
                RouteLeg("S3", D.DOWN, 10),
            ),
            tide_windows=((600, 700),),
            start_location="A", end_location="B",
        )
        self.assertEqual(t.segment_names, ("S1", "S2", "S3"))
        occ = t.leg_occupancy(600)
        self.assertEqual(occ["S1"], (600, 620, D.UP))
        self.assertEqual(occ["S2"], (620, 650, D.UP))
        self.assertEqual(occ["S3"], (650, 660, D.DOWN))
        self.assertTrue(self.topo.is_continuous(t.segment_names))

    def test_opposite_on_any_leg_blocks_other_segments_parallel(self):
        # M1 上行贯穿 S1/S2；N1 只在 S2 下行并与 M1 在 S2 上时间重叠 → 冲突；
        # N2 仅占用 S3，与 M1 在 S3 的方向虽相反，但时间窗允许前后排开。
        m1 = PilotageTask(
            "M1",
            (RouteLeg("S1", D.UP, 30), RouteLeg("S2", D.UP, 30)),
            ((600, 600),), "A", "B")
        n1 = PilotageTask(
            "N1", (RouteLeg("S2", D.DOWN, 30),),
            ((620, 620),), "B", "C")
        result = self.sch.solve([m1, n1])
        self.assertFalse(result.feasible)
        c = next(c for c in result.conflicts if c.kind == CONFLICT_OPPOSITE)
        self.assertEqual(c.resource, "S2")  # 冲突点精确定位到具体航段
        self.assertEqual({c.candidate_task, c.blocking_task}, {"M1", "N1"})

        # 只占用 S1 的另一下行任务，时间窗给足，可与某任务在 S2 上并行
        n2 = PilotageTask(
            "N2", (RouteLeg("S3", D.UP, 20),),
            ((600, 700),), "C", "D")
        result2 = self.sch.solve([m1, n2])
        self.assertTrue(result2.feasible)

    def test_segment_headway_override(self):
        rules = SafetyRules(same_direction_headway=60,
                            opposite_clearance=10,
                            segment_headways={"S1": 15})
        sch = Scheduler(self.topo, rules,
                        pilots=[Pilot("P1"), Pilot("P2")],
                        boats=[Boat("B1"), Boat("B2")],
                        time_step=5)
        # S1 覆盖为 15 分钟：间隔 15 合法
        a = PilotageTask("A", (RouteLeg("S1", D.UP, 20),),
                         ((600, 600),), "X", "Y")
        b = PilotageTask("B", (RouteLeg("S1", D.UP, 20),),
                         ((615, 615),), "X", "Y")
        self.assertTrue(sch.solve([a, b]).feasible)

        # S2 仍是全局 60：间隔 15 不合法，且窗口内拉不开
        c1 = PilotageTask("C1", (RouteLeg("S2", D.UP, 20),),
                          ((600, 620),), "X", "Y")
        c2 = PilotageTask("C2", (RouteLeg("S2", D.UP, 20),),
                          ((600, 615),), "X", "Y")
        r = sch.solve([c1, c2])
        self.assertFalse(r.feasible)
        self.assertTrue(any(c.kind == CONFLICT_HEADWAY and c.resource == "S2"
                            for c in r.conflicts))

    def test_one_way_segment_blocks_same_direction_overlap(self):
        topo = WaterwayTopology.of([Segment("OW", one_way=True)])
        sch = Scheduler(topo, SafetyRules(30, 10),
                        pilots=[Pilot("P1"), Pilot("P2")],
                        boats=[Boat("B1"), Boat("B2")],
                        time_step=5)
        a = PilotageTask("A", (RouteLeg("OW", D.UP, 30),),
                         ((600, 600),), "X", "Y")
        b = PilotageTask("B", (RouteLeg("OW", D.UP, 30),),
                         ((610, 610),), "X", "Y")
        r = sch.solve([a, b])
        self.assertFalse(r.feasible)
        self.assertTrue(any(c.resource == "OW" for c in r.conflicts))

    def test_tide_window_cross_midnight_ok(self):
        # 潮窗本身跨午夜：23:30..次日 00:30
        t = PilotageTask("T", (RouteLeg("S1", D.UP, 30),),
                         ((23 * 60 + 30, 24 * 60 + 30),), "A", "B")
        r = self.sch.solve([t])
        self.assertTrue(r.feasible)


class SnapshotRoundTripTests(unittest.TestCase):

    def test_rules_and_topology_round_trip(self):
        topo = WaterwayTopology.of([
            Segment("S1", 5.0, headway=12), Segment("S2")])
        topo.connect("S1", "S2")
        rules = SafetyRules(45, 15, False, {"S2": 9})
        snap_topo = WaterwayTopology.from_snapshot(topo.to_snapshot())
        self.assertTrue(snap_topo.is_continuous(["S1", "S2"]))
        self.assertEqual(snap_topo.segment("S1").headway, 12)
        data = {
            "same_direction_headway": rules.same_direction_headway,
            "opposite_clearance": rules.opposite_clearance,
            "forbid_opposite": rules.forbid_opposite,
            "segment_headways": dict(rules.segment_headways),
        }
        from pilot_sched.plan import _rules_from_dict
        restored = _rules_from_dict(data)
        self.assertEqual(restored.same_direction_headway, 45)
        self.assertFalse(restored.forbid_opposite)
        self.assertEqual(restored.headway_for("S2"), 9)
        self.assertEqual(restored.headway_for("S1"), 45)

    def test_lock_failure_writes_nothing(self):
        topo = WaterwayTopology.of([Segment("S1")])
        sch = Scheduler(topo, SafetyRules(30, 10),
                        pilots=[Pilot("P1"), Pilot("P2")],
                        boats=[Boat("B1"), Boat("B2")],
                        time_step=10)
        a = PilotageTask("A", (RouteLeg("S1", D.UP, 60),),
                         ((720, 780),), "X", "Y")
        b = PilotageTask("B", (RouteLeg("S1", D.DOWN, 60),),
                         ((720, 780),), "X", "Y")
        with tempfile.TemporaryDirectory() as d:
            store = PlanStore(d)
            with self.assertRaises(RevisionError):
                store.lock("BAD", [a, b], sch)
            self.assertEqual(store.versions("BAD"), [])
            self.assertEqual(os.listdir(d), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
