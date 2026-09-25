"""验收测试（映射到需求中的五项验收 + 统一模型的附加约束）：

  A1 人员和艇都够时，同段反向会遇仍被阻止；冲突返回航段/时间段/阻塞任务；
  A2 同段同向满足 headway 可以通过，不满足则被阻止；
  A3 不同航段任务可以并行（即使人员/艇资源充足且潮窗重叠）；
  A4 跨午夜航段占用被正确保留；
  A5 修订失败不写半成品；旧版本始终可查询；拓扑/规则变化只经显式修订生效。
  A6 联合模型：潮窗、人员、艇、转场同时生效。
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pilot_sched import (
    TimeKeeper,
    Direction, Segment, WaterwayTopology, SafetyRules,
    RouteLeg, PilotageTask, Pilot, Boat,
    Scheduler,
    PlanStore, RevisionError, PlanNotFound,
)
from pilot_sched.scheduler import (
    CONFLICT_OPPOSITE, CONFLICT_HEADWAY, CONFLICT_TRANSFER,
    CONFLICT_TIDE, CONFLICT_PILOT,
)

D = Direction


def build_topology():
    """两段相邻虚构航道：S1 -- S2。"""
    s1 = Segment("S1", length_nm=5.0)
    s2 = Segment("S2", length_nm=5.0)
    topo = WaterwayTopology.of([s1, s2])
    topo.connect("S1", "S2")
    return topo


def task(tid, segment, direction, window, *, duration=60,
         start_loc="ANCH", end_loc="BERTH", pickup=0, dropoff=0):
    if isinstance(segment, str):
        legs = (RouteLeg(segment, direction, duration),)
    else:
        legs = tuple(RouteLeg(s, d, dur) for s, d, dur in segment)
    return PilotageTask(
        id=tid, legs=legs, tide_windows=(window,),
        start_location=start_loc, end_location=end_loc,
        pickup_minutes=pickup, dropoff_minutes=dropoff,
    )


class WaterwayAcceptance(unittest.TestCase):

    def setUp(self):
        self.tk = TimeKeeper("2026-09-24")
        self.topo = build_topology()
        self.rules = SafetyRules(
            same_direction_headway=30, opposite_clearance=10)
        self.pilots = [Pilot("P1"), Pilot("P2"), Pilot("P3")]
        self.boats = [Boat("B1"), Boat("B2"), Boat("B3")]

    def make_scheduler(self, *, rules=None, pilots=None, boats=None,
                       transfers=None, time_step=10):
        return Scheduler(
            topology=self.topo,
            rules=rules or self.rules,
            pilots=pilots if pilots is not None else self.pilots,
            boats=boats if boats is not None else self.boats,
            transfer_minutes=transfers or {},
            time_step=time_step,
        )

    # ---- A1: 反向会遇必须被阻止（人员和艇都够） -----------------------

    def test_A1_opposite_meeting_blocked_despite_enough_crew_and_boats(self):
        sch = self.make_scheduler(time_step=10)
        # 两个 60 分钟反向任务先后排列共需 60+10+60=130 分钟；
        # 窗口为 12:00..13:00（60 分钟宽），任何先后排列都必须会遇。
        win = (12 * 60, 13 * 60)
        t1 = task("T1", "S1", D.UP, win, duration=60)
        t2 = task("T2", "S1", D.DOWN, win, duration=60)

        result = sch.solve([t1, t2])
        self.assertFalse(result.feasible, "反向会遇应不可行")

        # 冲突响应：具体航段、时间段、阻塞任务
        c = next(c for c in result.conflicts if c.kind == CONFLICT_OPPOSITE)
        self.assertEqual(c.resource, "S1")
        self.assertEqual({c.candidate_task, c.blocking_task}, {"T1", "T2"})
        self.assertIsNotNone(c.interval)
        self.assertIsNotNone(c.blocking_interval)
        # 时间段在两个潮窗覆盖的范围内
        self.assertGreaterEqual(c.interval[0], 12 * 60)
        self.assertLessEqual(c.interval[1], 15 * 60)

        # check_placement 对具体排班给出同样的解释
        from pilot_sched import Assignment
        plan = {"T1": Assignment("T1", 13 * 60, "P1", "B1")}
        tasks = {"T1": t1}
        cs = sch.check_placement(t2, 13 * 60, "P2", "B2", plan, tasks)
        self.assertTrue(any(c.kind == CONFLICT_OPPOSITE
                            and c.resource == "S1"
                            and c.blocking_task == "T1" for c in cs))

    def test_A1b_clearance_buffer_enforced(self):
        """不仅不能重叠，反向任务之间还必须留 opposite_clearance 安全余量。"""
        sch = self.make_scheduler(time_step=10)
        # T1 可 12:00..13:00 进入（12:00 进 -> 13:00 离开）；
        # T2 只能 12:00..12:10 进入：反向先后排序要求 T2 最早 13:10 进入，
        # 正向先后排序要求 T1 最晚 11:50 进入——两种顺序都不可能。
        t1 = task("T1", "S1", D.UP, (12 * 60, 13 * 60), duration=60)
        t2 = task("T2", "S1", D.DOWN, (12 * 60, 12 * 60 + 10), duration=60)
        result = sch.solve([t1, t2])
        self.assertFalse(result.feasible)
        self.assertTrue(any(c.kind == CONFLICT_OPPOSITE
                            and c.resource == "S1" for c in result.conflicts))

    # ---- A2: 同向满足 headway 可通过，不满足被阻止 --------------------

    def test_A2_same_direction_headway(self):
        # 30 分钟 headway：12:00 与 12:30 同向可排
        sch = self.make_scheduler(time_step=10)
        win = (12 * 60, 13 * 60)
        t1 = task("T1", "S1", D.UP, win, duration=60)
        t2 = task("T2", "S1", D.UP, win, duration=60)
        result = sch.solve([t1, t2])
        self.assertTrue(result.feasible, "同向间隔 30 分钟应可通过")
        starts = sorted(a.start for a in result.assignments.values())
        self.assertEqual(starts[1] - starts[0], 30)

    def test_A2b_headway_violation_blocked(self):
        # headway 收紧到 60 分钟：T1 可 12:10..12:50 进入，T2 只能 12:00，
        # 进入时刻最大相差 50 分钟（任意先后顺序），无法达到 60
        rules = SafetyRules(same_direction_headway=60,
                            opposite_clearance=10)
        sch = self.make_scheduler(rules=rules, time_step=10)
        t1 = task("T1", "S1", D.UP, (12 * 60 + 10, 12 * 60 + 50),
                  duration=60)
        t2 = task("T2", "S1", D.UP, (12 * 60, 12 * 60), duration=60)
        result = sch.solve([t1, t2])
        self.assertFalse(result.feasible)
        c = next(c for c in result.conflicts if c.kind == CONFLICT_HEADWAY)
        self.assertEqual(c.resource, "S1")
        self.assertEqual({c.candidate_task, c.blocking_task}, {"T1", "T2"})
        self.assertIsNotNone(c.interval)

    # ---- A3: 不同航段可并行 -------------------------------------------

    def test_A3_different_segments_run_parallel(self):
        sch = self.make_scheduler(time_step=10)
        win = (12 * 60, 13 * 60)
        t1 = task("T1", "S1", D.UP, win, duration=60)
        t2 = task("T2", "S2", D.DOWN, win, duration=60)
        result = sch.solve([t1, t2])
        self.assertTrue(result.feasible, "不同航段即使反向也应可并行")
        self.assertEqual(result.assignments["T1"].start,
                         result.assignments["T2"].start,
                         "两任务应能在同一时刻并行")

    # ---- A4: 跨午夜航段占用正确保留 -----------------------------------

    def test_A4_cross_midnight_occupancy(self):
        sch = self.make_scheduler(time_step=10)
        # T1 23:30 进入 S1，航行 90 分钟 -> 次日 01:00 离开
        t1 = task("T1", "S1", D.UP, (23 * 60 + 30, 23 * 60 + 30),
                  duration=90)
        occ = t1.leg_occupancy(23 * 60 + 30)["S1"]
        self.assertEqual(occ[0], 23 * 60 + 30)
        self.assertEqual(occ[1], 24 * 60 + 60)  # 次日 01:00 = 1500

        # 反向 T2 即便在午夜之后 00:30 进入，仍与 T1 在 S1 上会遇
        t2 = task("T2", "S1", D.DOWN, (24 * 60 + 30, 24 * 60 + 30),
                  duration=60)
        result = sch.solve([t1, t2])
        self.assertFalse(result.feasible, "跨午夜的反向会遇必须被识别")
        c = next(c for c in result.conflicts if c.kind == CONFLICT_OPPOSITE)
        self.assertEqual(c.resource, "S1")
        # 冲突时间段跨越午夜边界
        self.assertGreaterEqual(c.interval[0], 24 * 60)
        self.assertLess(c.interval[1], 25 * 60 + 60)

    def test_A4b_cross_midnight_serialized_in_locked_plan(self):
        """锁定计划落盘后，跨午夜占用仍保持绝对分钟（不做 mod 1440）。"""
        sch = self.make_scheduler(time_step=10)
        t1 = task("T1", "S1", D.UP, (23 * 60 + 30, 24 * 60 + 30),
                  duration=90)
        with tempfile.TemporaryDirectory() as d:
            store = PlanStore(d)
            locked = store.lock("PLAN-X", [t1], sch, note="夜班")
            self.assertEqual(locked.assignment_for("T1").start,
                             23 * 60 + 30)
            reloaded = store.get("PLAN-X")
            self.assertEqual(reloaded.assignment_for("T1").start,
                             23 * 60 + 30)
            restored_task = reloaded.tasks["T1"]
            enter, leave, _ = restored_task.leg_occupancy(23 * 60 + 30)["S1"]
            self.assertEqual(leave - enter, 90)
            self.assertEqual(leave - enter, 90)
            self.assertEqual(leave, 24 * 60 + 60)

    # ---- A6: 潮窗/人员/艇/转场 联合模型 -------------------------------

    def test_A6_transfer_plus_tide_and_resources_together(self):
        # 只有 1 名引航员、1 艘艇；BERTH->ANCH 转场需要 20 分钟
        sch = self.make_scheduler(
            pilots=[Pilot("P1")], boats=[Boat("B1")],
            transfers={("BERTH", "ANCH"): 20},
            time_step=5,
        )
        # T1 12:00 S1 UP（ANCH->BERTH，60 分钟）
        t1 = task("T1", "S1", D.UP, (12 * 60, 12 * 60),
                  start_loc="ANCH", end_loc="BERTH", duration=60)
        # T2 潮窗最晚 13:10；从 BERTH 回 ANCH 需转场 20 分钟，
        # 最早 13:20 才能开始 -> 无可行解
        t2 = task("T2", "S1", D.UP, (13 * 60, 13 * 60 + 10),
                  start_loc="ANCH", end_loc="BERTH", duration=60)
        result = sch.solve([t1, t2])
        self.assertFalse(result.feasible)
        self.assertTrue(
            any(c.kind == CONFLICT_TRANSFER
                and c.blocking_task == "T1"
                and c.resource in ("P1", "B1")
                for c in result.conflicts)
            or any(c.kind == CONFLICT_TIDE for c in result.conflicts),
            f"应报告转场或潮窗冲突，实际: {result.conflicts}")

        # 把潮窗放宽到 13:30，且 T2 同向 → 可排（12:00 + 60 航行 + 20 转场 = 13:20）
        t2b = task("T2", "S1", D.UP, (13 * 60, 14 * 60),
                   start_loc="ANCH", end_loc="BERTH", duration=60)
        result2 = sch.solve([t1, t2b])
        self.assertTrue(result2.feasible)
        self.assertEqual(result2.assignments["T1"].pilot_id,
                         result2.assignments["T2"].pilot_id)
        self.assertEqual(result2.assignments["T2"].start, 13 * 60 + 20)

    def test_A6b_tide_blocks_even_with_idle_resource(self):
        sch = self.make_scheduler(time_step=10)
        t1 = task("T1", "S1", D.UP, (12 * 60, 12 * 60), duration=30)
        result = sch.solve([t1])
        self.assertTrue(result.feasible)
        # 同一任务潮窗不覆盖的时刻应被直接拒绝
        cs = sch.check_placement(
            t1, 18 * 60, "P1", "B1", {}, {})
        self.assertTrue(any(c.kind == CONFLICT_TIDE for c in cs))

    def test_A6c_pilot_overlap_blocks_when_only_one_pilot(self):
        """不同航段可并行，但只有一名引航员时，人员占用重叠仍被阻止——
        航道规则放行不等于人员约束放行（五类约束在同一模型中合取）。"""
        sch = self.make_scheduler(
            pilots=[Pilot("P1")], boats=self.boats,
            transfers={}, time_step=10)
        # T1 走 S1 60 分钟；T2 走 S2（航段层面可并行）但同一引航员无法分身
        t1 = task("T1", "S1", D.UP, (12 * 60, 12 * 60), duration=60)
        t2 = task("T2", "S2", D.UP, (12 * 60 + 30, 12 * 60 + 30),
                  duration=60)
        result = sch.solve([t1, t2])
        self.assertFalse(result.feasible)
        self.assertTrue(
            any(c.kind == CONFLICT_PILOT and c.resource == "P1"
                for c in result.conflicts))

        # 第二名引航员加入后即可并行
        sch2 = self.make_scheduler(
            pilots=[Pilot("P1"), Pilot("P2")], boats=self.boats,
            time_step=10)
        result2 = sch2.solve([t1, t2])
        self.assertTrue(result2.feasible)


class PlanRevisionAcceptance(unittest.TestCase):
    """A5: 锁定快照 + 显式修订 + 原子性 + 旧版本可查询。"""

    def setUp(self):
        self.topo = build_topology()
        self.rules = SafetyRules(same_direction_headway=30,
                                 opposite_clearance=10)
        self.pilots = [Pilot("P1"), Pilot("P2")]
        self.boats = [Boat("B1"), Boat("B2")]
        self.t1 = task("T1", "S1", D.UP, (12 * 60 + 10, 12 * 60 + 50),
                       duration=60)
        # T2 仅能 12:00 进入：headway 30 可排（T2 12:00 / T1 12:40），
        # headway 60 时进入时刻最大相差 50 分钟，无任何可行排列
        self.t2 = task("T2", "S1", D.UP, (12 * 60, 12 * 60), duration=60)
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PlanStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _sch(self, **kw):
        return Scheduler(
            topology=kw.get("topology", self.topo),
            rules=kw.get("rules", self.rules),
            pilots=kw.get("pilots", self.pilots),
            boats=kw.get("boats", self.boats),
            transfer_minutes={},
            time_step=kw.get("time_step", 10),
        )

    def test_A5_lock_then_revise_versions_immutable(self):
        v1 = self.store.lock("P", [self.t1, self.t2], self._sch())
        self.assertEqual(v1.version, 1)
        s1 = v1.assignment_for("T1").start
        s2 = v1.assignment_for("T2").start
        self.assertEqual(abs(s2 - s1), 30)

        # 显式修订：headway 收紧为 60，但潮窗只到 13:00（12:00 起的可行域内
        # 拉不开 60 分钟）→ 修订必须失败
        strict = SafetyRules(same_direction_headway=60,
                             opposite_clearance=10)
        with self.assertRaises(RevisionError) as cm:
            self.store.revise("P", rules=strict, note="收紧 headway")
        self.assertTrue(any(c["kind"] == CONFLICT_HEADWAY
                            for c in cm.exception.conflicts))

        # 失败不写半成品：磁盘上仍只有 v1
        self.assertEqual(self.store.versions("P"), [1])
        files = sorted(os.listdir(os.path.join(self.tmp.name, "P")))
        self.assertEqual(files, ["v1.json"])

        # 旧版本始终可查询（显式版本号 / 最新版本一致）
        got = self.store.get("P", 1)
        self.assertEqual(got.version, 1)
        self.assertEqual(self.store.get("P").version, 1)
        self.assertEqual(got.assignment_for("T1").start, s1)
        self.assertEqual(self.store.get_all("P")[0].version, 1)

        # 可行的显式修订（放宽潮窗/任务）后产生 v2，v1 原样保留
        t2b = task("T2", "S1", D.UP, (12 * 60, 14 * 60), duration=60)
        v2 = self.store.revise(
            "P", rules=strict, tasks=[self.t1, t2b], note="headway 60")
        self.assertEqual(v2.version, 2)
        self.assertEqual(v2.revisions[0].based_on_version, 1)
        self.assertEqual(self.store.versions("P"), [1, 2])
        self.assertEqual(self.store.get("P", 1).assignment_for("T2").start,
                         s2)  # 旧版本排班不变
        new_starts = sorted(
            a.start for a in self.store.get("P", 2).assignments.values())
        self.assertEqual(new_starts[1] - new_starts[0], 60)

    def test_A5_snapshot_isolates_later_topology_changes(self):
        """锁定后在线拓扑变化不影响旧版本；只有显式修订才生效。"""
        locked = self.store.lock("P", [self.t1], self._sch())
        # 在线把 S1 改成单航段水道、改长度——内存对象层面的变化
        from pilot_sched.topology import Segment as Seg
        self.topo.segments["S1"] = Seg("S1", length_nm=99.0, one_way=True)

        snap = locked.snapshot_topology()
        self.assertAlmostEqual(snap.segment("S1").length_nm, 5.0)
        self.assertFalse(snap.segment("S1").one_way)
        # 从磁盘重新读出的版本同样使用旧快照
        self.assertAlmostEqual(
            self.store.get("P").snapshot_topology().segments["S1"].length_nm,
            5.0)

    def test_A5_missing_plan_and_duplicate_lock(self):
        with self.assertRaises(PlanNotFound):
            self.store.get("NOPE")
        self.store.lock("P", [self.t1], self._sch())
        with self.assertRaises(RevisionError):
            self.store.lock("P", [self.t1], self._sch())

    def test_A5_revise_add_task_then_rollback_on_failure(self):
        self.store.lock("P", [self.t1], self._sch())
        # 新增一个必然与 T1 反向会遇的任务 → 修订失败，v1 仍可用
        clash = task("T9", "S1", D.DOWN, (12 * 60, 12 * 60 + 30),
                     duration=60)
        with self.assertRaises(RevisionError):
            self.store.revise("P", tasks=[self.t1, clash])
        self.assertEqual(self.store.versions("P"), [1])
        v1 = self.store.get("P")
        self.assertEqual(set(v1.assignments), {"T1"})
        # 规则快照也随版本冻结
        self.assertEqual(v1.snapshot_rules().same_direction_headway, 30)


if __name__ == "__main__":
    unittest.main(verbosity=2)
