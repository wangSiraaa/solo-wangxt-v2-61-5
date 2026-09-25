"""验收测试 5：锁定快照、显式修订、失败不留半成品、旧版本始终可查。

覆盖
----
* lock_plan 成功落盘 v1，保存拓扑/航段规则/潮窗/人员/艇/转场快照；
* 锁定后修改内存拓扑（改 headway、加航段）不影响已锁计划；
* 修订必须显式提交：成功产生新版本并切换锁指针；
* 修订失败（加入必然会遇的任务）不写任何半成品文件，
  get_locked / get_locked_version / get_plan(v1) 仍返回旧版本；
* 失败后可继续提交另一成功修订，版本号连续不跳号、不残留临时文件；
* 可基于任意旧版本分叉修订（新版本号全局递增，不覆盖旧版本）。
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta

from pilot_schedule import (
    DOWN,
    Infeasible,
    PilotageTask,
    PlanStore,
    Problem,
    RevisionSpec,
    RouteStep,
    Segment,
    TideWindow,
    UP,
)
from pilot_schedule.constraints import SEGMENT_HEADON
from pilot_schedule.fixtures import (
    build_demo_topology,
    demo_relocations,
    make_boats,
    make_pilots,
)

DAY = datetime(2026, 9, 24)
TIDE = [
    TideWindow.from_datetimes(
        DAY - timedelta(hours=2), DAY + timedelta(hours=26), "宽潮窗"
    )
]


def _s1_task(tid: str, vessel: str, h: int, m: int = 0,
             direction: int = UP, window_min: int = 0,
             origin: str = "A", destination: str = "B") -> PilotageTask:
    start = DAY.replace(hour=h, minute=m)
    return PilotageTask.from_route(
        tid, vessel,
        (RouteStep("S1", direction, 0, 40),),
        start - timedelta(minutes=window_min),
        start + timedelta(minutes=window_min),
        origin=origin, destination=destination,
        start_granularity_min=10,
    )


def make_problem(topo, tasks):
    return Problem(
        topology=topo,
        tasks=tasks,
        tide_windows=TIDE,
        pilots=make_pilots(4),
        boats=make_boats(3),
        pilot_relocations=demo_relocations(),
        boat_relocations=demo_relocations(),
    )


class LockRevisionAcceptanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="plans-")
        self.store = PlanStore(self.tmpdir)
        self.topo = build_demo_topology()

        # T1：S1 上行，08:00 ±20 分钟内开始
        self.t1 = _s1_task("T1", "货轮一", 8, window_min=20)
        # T2：S1 同向（上行），09:00 钉死 —— 与最早 07:40 的 T1 也相差
        # 80 分钟 >= headway 30，修订加入后必然可解
        self.t2 = _s1_task("T2", "货轮二", 9)

    def _plan_file(self, v: int) -> str:
        return os.path.join(self.tmpdir, "plans", f"v{v}.json")

    def _no_temp_files(self) -> None:
        for _root, _dirs, files in os.walk(self.tmpdir):
            for name in files:
                self.assertFalse(
                    name.endswith(".tmp") or name.startswith(".tmp"),
                    f"发现半成品临时文件: {name}",
                )

    # ---- 1. 锁定保存快照 ----
    def test_lock_persists_topology_rule_snapshot(self) -> None:
        plan = self.store.lock_plan("PLAN-1", make_problem(self.topo, [self.t1]))
        self.assertEqual(plan.revision_no, 1)
        self.assertEqual(self.store.get_locked_version(), 1)

        # 快照内航段规则正确
        self.assertEqual(
            plan.topology_snapshot.segment("S1").same_direction_headway, 30
        )

        # 磁盘 JSON 内含完整拓扑与规则快照
        with open(self._plan_file(1), encoding="utf-8") as fh:
            raw = json.load(fh)
        self.assertEqual(
            raw["topology"]["segments"]["S2"]["same_direction_headway"], 20
        )
        self.assertEqual(raw["topology"]["segments"]["S3"]
                         ["same_direction_headway"], 40)
        self.assertEqual(len(raw["rules"]["tide_windows"]), 1)
        self.assertEqual(len(raw["rules"]["pilots"]), 4)
        self.assertEqual(len(raw["rules"]["boats"]), 3)
        self.assertTrue(raw["rules"]["pilot_relocations"]["travel"])
        # 航段占用也持久化
        self.assertEqual(raw["assignments"][0]["occupancies"][0]
                         ["segment_code"], "S1")

    # ---- 2. 锁后外部拓扑变化不影响计划 ----
    def test_post_lock_topology_change_does_not_touch_snapshot(self) -> None:
        locked_assignment = self.store.lock_plan(
            "PLAN-1", make_problem(self.topo, [self.t1])
        ).get_assignment("T1")

        # 内存拓扑被改动：S1 headway 改成 5，并新增 S9
        self.topo.segments["S1"] = Segment(
            code="S1", name="进港航道-改造", same_direction_headway=5
        )
        self.topo.add_segment(Segment(code="S9", name="新增航段"))

        locked = self.store.get_locked()
        self.assertEqual(
            locked.topology_snapshot.segment("S1").same_direction_headway, 30
        )
        self.assertNotIn("S9", locked.topology_snapshot.segments)
        # 已锁排班结果不变
        still = locked.get_assignment("T1")
        self.assertEqual(still.start_min, locked_assignment.start_min)

    # ---- 3. 显式修订成功产生 v2 ----
    def test_explicit_revision_creates_new_version(self) -> None:
        self.store.lock_plan("PLAN-1", make_problem(self.topo, [self.t1]))

        result = self.store.revise(
            None, RevisionSpec(note="加靠 T2", add_tasks=[self.t2])
        )
        self.assertNotIsInstance(result, Infeasible)
        self.assertEqual(result.revision_no, 2)
        self.assertEqual(self.store.get_locked_version(), 2)

        # 旧版本 v1 始终可查询，且内容保持原样
        v1 = self.store.get_plan(1)
        self.assertIn("T1", v1.schedule.assignments)
        self.assertNotIn("T2", v1.schedule.assignments)
        self.assertEqual(v1.note, "initial lock")

        v2 = self.store.get_plan(2)
        self.assertIn("T2", v2.schedule.assignments)
        self.assertEqual(self.store.list_revisions(), [1, 2])
        self._no_temp_files()

    # ---- 4. 修订失败不写半成品，旧版本仍可查 ----
    def test_failed_revision_writes_nothing_and_old_version_queryable(self) -> None:
        self.store.lock_plan("PLAN-1", make_problem(self.topo, [self.t1]))
        plans_dir = os.path.join(self.tmpdir, "plans")
        files_before = set(os.listdir(plans_dir))

        # T-BAD：S1 反向，08:00 钉死。T1 即便取最早 07:40，其 S1 占用
        # 07:40~08:20 仍与 T-BAD 的 08:00~08:40 重叠 -> 必然会遇无解。
        t_bad = _s1_task("T-BAD", "逆行轮", 8, direction=DOWN,
                         origin="B", destination="A")
        result = self.store.revise(
            None, RevisionSpec(note="必然会遇的修订", add_tasks=[t_bad])
        )
        self.assertIsInstance(result, Infeasible)
        headon = [c for c in result.conflicts if c.kind == SEGMENT_HEADON]
        self.assertTrue(headon)
        c = headon[0]
        self.assertEqual(c.segment_code, "S1")
        # 会遇是对称的：双方互为阻塞者（诊断从后放置的一方视角报告）
        self.assertEqual({c.task_id, c.blocking_task_id}, {"T1", "T-BAD"})
        self.assertIsNotNone(c.interval_start_min)
        self.assertLess(c.interval_start_min, c.interval_end_min)

        # 没有新版本文件、没有临时文件、清单与锁指针仍停在 v1
        self.assertEqual(set(os.listdir(plans_dir)), files_before)
        self.assertFalse(os.path.exists(self._plan_file(2)))
        self.assertEqual(self.store.get_locked_version(), 1)
        self.assertEqual(self.store.list_revisions(), [1])

        # 旧版本仍完整可查
        v1 = self.store.get_plan(1)
        self.assertIn("T1", v1.schedule.assignments)
        self.assertEqual(self.store.get_locked().revision_no, 1)
        self._no_temp_files()

    # ---- 5. 失败后再提交成功修订，版本号连续为 v2 ----
    def test_successful_revision_after_failure_is_v2(self) -> None:
        self.store.lock_plan("PLAN-1", make_problem(self.topo, [self.t1]))

        t_bad = _s1_task("T-BAD", "逆行轮", 8, direction=DOWN,
                         origin="B", destination="A")
        failed = self.store.revise(
            None, RevisionSpec(note="bad", add_tasks=[t_bad])
        )
        self.assertIsInstance(failed, Infeasible)

        ok = self.store.revise(
            None, RevisionSpec(note="ok", add_tasks=[self.t2])
        )
        self.assertNotIsInstance(ok, Infeasible)
        self.assertEqual(ok.revision_no, 2)  # 失败不留号，成功才占 v2
        self.assertEqual(self.store.list_revisions(), [1, 2])
        self.assertEqual(self.store.get_locked_version(), 2)
        self._no_temp_files()

    # ---- 6. 可基于旧版本分叉修订，新版本号全局递增 ----
    def test_revision_can_be_based_on_explicit_old_version(self) -> None:
        self.store.lock_plan("PLAN-1", make_problem(self.topo, [self.t1]))
        self.store.revise(None, RevisionSpec(note="v2", add_tasks=[self.t2]))
        self.assertEqual(self.store.get_locked_version(), 2)

        # 基于 v1 分叉：只加 t3（11:00 上行），生成 v3（不是覆盖 v2）
        t3 = _s1_task("T3", "货轮三", 11)
        result = self.store.revise(
            1, RevisionSpec(note="基于v1分叉", add_tasks=[t3])
        )
        self.assertNotIsInstance(result, Infeasible)
        self.assertEqual(result.revision_no, 3)
        self.assertEqual(self.store.list_revisions(), [1, 2, 3])

        v3 = self.store.get_plan(3)
        self.assertIn("T1", v3.schedule.assignments)
        self.assertIn("T3", v3.schedule.assignments)
        self.assertNotIn("T2", v3.schedule.assignments)
        # v2 旧版本始终可查
        self.assertIn("T2", self.store.get_plan(2).schedule.assignments)


if __name__ == "__main__":
    unittest.main()
