"""端到端演示：离线虚构航道拓扑排班 + 锁定/修订。

运行：PYTHONPATH=. python3 examples/demo.py
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta

from pilot_schedule import (
    DOWN,
    Infeasible,
    PilotageTask,
    PlanStore,
    Problem,
    RevisionSpec,
    RouteStep,
    TideWindow,
    UP,
    solve,
)
from pilot_schedule.fixtures import (
    branch_steps,
    build_demo_topology,
    demo_relocations,
    inbound_steps,
    make_boats,
    make_pilots,
)
from pilot_schedule.time_utils import clock_label

DAY = datetime(2026, 9, 24)
TIDE = [TideWindow.from_datetimes(
    DAY - timedelta(hours=2), DAY + timedelta(hours=26), "宽潮窗"
)]


def line(title: str) -> None:
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


def show_conflicts(result: Infeasible) -> None:
    print("  无解。冲突响应（具体航段/时间段/阻塞任务）：")
    for c in result.conflicts:
        when = ""
        if c.interval_start_min is not None:
            when = (
                f" | 时段 {clock_label(c.interval_start_min)}~"
                f"{clock_label(c.interval_end_min)}"
            )
        seg = f" | 航段 {c.segment_code}" if c.segment_code else ""
        blocker = (
            f" | 阻塞任务 {c.blocking_task_id}" if c.blocking_task_id else ""
        )
        print(f"   - [{c.kind}] 任务 {c.task_id}{seg}{when}{blocker}")
        print(f"       {c.message}")


def main() -> None:
    topo = build_demo_topology()
    pilots, boats, reloc = make_pilots(4), make_boats(3), demo_relocations()

    def problem(tasks):
        return Problem(
            topology=topo, tasks=tasks, tide_windows=TIDE,
            pilots=pilots, boats=boats,
            pilot_relocations=reloc, boat_relocations=reloc,
        )

    # 1) 人员/艇充足，同段反向会遇仍被阻止 -------------------------------
    line("1) 反向会遇：4 名引航员 + 3 艘艇全部空闲，仍然阻止")
    t_up = PilotageTask.from_route(
        "UP", "上行轮", (RouteStep("S1", UP, 0, 40),),
        DAY.replace(hour=8), DAY.replace(hour=8), origin="A", destination="B")
    t_down = PilotageTask.from_route(
        "DOWN", "下行轮", (RouteStep("S1", DOWN, 0, 40),),
        DAY.replace(hour=8), DAY.replace(hour=8), origin="B", destination="A")
    show_conflicts(solve(problem([t_up, t_down])))

    # 2) 同向满足 headway 可以通过 ---------------------------------------
    line("2) 同向 headway：08:00 与 08:30（S1 headway=30）通过")
    t2 = PilotageTask.from_route(
        "T2", "货轮二", inbound_steps(),
        DAY.replace(hour=8, minute=30), DAY.replace(hour=8, minute=30),
        origin="A", destination="C")
    t1 = PilotageTask.from_route(
        "T1", "货轮一", inbound_steps(),
        DAY.replace(hour=8), DAY.replace(hour=8), origin="A", destination="C")
    ok = solve(problem([t1, t2]))
    print(f"  T1 引航员 {ok.get('T1').pilot_id} / 艇 {ok.get('T1').boat_id}")
    print(f"  T2 引航员 {ok.get('T2').pilot_id} / 艇 {ok.get('T2').boat_id}")

    # 3) 不同航段并行 ----------------------------------------------------
    line("3) 不同航段并行：S1/S2 进港 与 S3 支线同时开始")
    t_branch = PilotageTask.from_route(
        "TB", "支线驳船", branch_steps(),
        DAY.replace(hour=8), DAY.replace(hour=8), origin="B", destination="D")
    ok = solve(problem([t1, t_branch]))
    print(f"  干线开始 {clock_label(ok.get('T1').start_min)}，"
          f"支线开始 {clock_label(ok.get('TB').start_min)}（同一时刻）")

    # 4) 跨午夜 ----------------------------------------------------------
    line("4) 跨午夜占用：23:50 开始，S1 占用保留到次日 00:30")
    night_tide = [TideWindow.from_datetimes(
        DAY.replace(hour=20), DAY + timedelta(days=1, hours=8), "跨夜潮窗")]
    t_night = PilotageTask.from_route(
        "N1", "夜航轮", inbound_steps(),
        DAY.replace(hour=23, minute=50), DAY.replace(hour=23, minute=50),
        origin="A", destination="C")
    night_problem = Problem(
        topology=topo, tasks=[t_night], tide_windows=night_tide,
        pilots=pilots, boats=boats,
        pilot_relocations=reloc, boat_relocations=reloc)
    a = solve(night_problem).get("N1")
    for occ in a.occupancies:
        print(f"  航段 {occ.segment_code}: "
              f"{clock_label(occ.entry_min)} ~ {clock_label(occ.exit_min)}")

    # 5) 锁定 + 修订失败不留半成品 + 成功修订 -----------------------------
    line("5) 锁定 v1 -> 失败修订不落盘 -> 成功修订为 v2，v1 仍可查")
    with tempfile.TemporaryDirectory(prefix="plan-store-") as store_dir:
        store = PlanStore(store_dir)
        v1 = store.lock_plan("PLAN-X", problem([t1]))
        print(f"  已锁定 v{v1.revision_no}")

        bad_revision = store.revise(
            None, RevisionSpec(note="加入反向任务", add_tasks=[t_down]))
        assert isinstance(bad_revision, Infeasible)
        print(f"  失败修订：{bad_revision.message}")
        print(f"  当前锁定版本仍是：v{store.get_locked_version()}")

        store.revise(None, RevisionSpec(note="加靠 T2", add_tasks=[t2]))
        print(f"  成功修订后锁定：v{store.get_locked_version()}")
        old = store.get_plan(1)
        print(f"  旧版本 v1 仍可查，含任务："
              f"{sorted(old.schedule.assignments)}")


if __name__ == "__main__":
    main()
