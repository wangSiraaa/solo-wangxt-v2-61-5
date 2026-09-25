"""锁定计划：拓扑/规则快照 + 显式修订 + 版本管理。

关键语义
========
* ``lock_plan`` 求解成功后，把**当时的**拓扑、航段安全规则、潮窗、人员、
  接送艇、转场表连同排班结果整体序列化为不可变快照（v1, v2, ...）。
* 锁定后再修改内存中的拓扑（例如改 headway、加航段）不影响已锁定计划；
  任何变化必须通过 ``revise`` 显式提交修订才生效。
* 修订 = 基于旧快照重建问题 -> 应用修订 -> **整体重新求解** -> 成功才以
  “临时文件 + 原子改名 + 指针最后切换”的方式落盘。求解失败或写盘失败
  都不会产生半成品：指针不变，``get_plan/get_locked_version`` 永远可以
  查到修订前的旧版本。
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from typing import Optional

from .domain import (
    Boat,
    Pilot,
    PilotageTask,
    RelocationTable,
    TideWindow,
)
from .scheduler import (
    Assignment,
    Infeasible,
    Problem,
    Schedule,
    solve,
)
from .time_utils import iso, parse_iso, to_min
from .topology import (
    Route,
    RouteStep,
    Segment,
    Topology,
    TopologySnapshot,
)

SCHEMA_VERSION = 1
_INDEX_FILE = "index.json"
_LOCK_FILE = "lock.json"
_PLANS_DIR = "plans"


# ---------------------------------------------------------------------------
# 序列化辅助
# ---------------------------------------------------------------------------
def _segment_to_dict(s: Segment) -> dict:
    return {
        "code": s.code,
        "name": s.name,
        "same_direction_headway": s.same_direction_headway,
        "min_offset": s.min_offset,
        "bidirectional": s.bidirectional,
        "nautical_miles": s.nautical_miles,
    }


def _segment_from_dict(d: dict) -> Segment:
    return Segment(
        code=d["code"],
        name=d["name"],
        same_direction_headway=d["same_direction_headway"],
        min_offset=d["min_offset"],
        bidirectional=d["bidirectional"],
        nautical_miles=d.get("nautical_miles", 0.0),
    )


def _step_to_dict(step: RouteStep) -> dict:
    return {
        "segment_code": step.segment_code,
        "direction": step.direction,
        "entry_offset_min": step.entry_offset_min,
        "duration_min": step.duration_min,
    }


def _step_from_dict(d: dict) -> RouteStep:
    return RouteStep(
        segment_code=d["segment_code"],
        direction=int(d["direction"]),
        entry_offset_min=d["entry_offset_min"],
        duration_min=d["duration_min"],
    )


def _route_to_dict(r: Route) -> dict:
    return {
        "name": r.name,
        "steps": [_step_to_dict(s) for s in r.steps],
    }


def _route_from_dict(d: dict) -> Route:
    return Route(
        name=d["name"],
        steps=tuple(_step_from_dict(s) for s in d["steps"]),
    )


def _task_to_dict(t: PilotageTask) -> dict:
    return {
        "id": t.id,
        "vessel": t.vessel,
        "steps": [_step_to_dict(s) for s in t.steps],
        "earliest_start": iso(t.earliest_start_min),
        "latest_start": iso(t.latest_start_min),
        "tide_required": t.tide_required,
        "min_pilot_grade": t.min_pilot_grade,
        "boat_required": t.boat_required,
        "origin": t.origin,
        "destination": t.destination,
        "start_granularity_min": t.start_granularity_min,
    }


def _task_from_dict(d: dict) -> PilotageTask:
    return PilotageTask(
        id=d["id"],
        vessel=d["vessel"],
        steps=tuple(_step_from_dict(s) for s in d["steps"]),
        earliest_start_min=to_min(parse_iso(d["earliest_start"])),
        latest_start_min=to_min(parse_iso(d["latest_start"])),
        tide_required=d["tide_required"],
        min_pilot_grade=d["min_pilot_grade"],
        boat_required=d["boat_required"],
        origin=d.get("origin"),
        destination=d.get("destination"),
        start_granularity_min=d.get("start_granularity_min", 10),
    )


def _assignment_to_dict(a: Assignment) -> dict:
    return {
        "task_id": a.task_id,
        "start_min": a.start_min,
        "end_min": a.end_min,
        "start": iso(a.start_min),
        "end": iso(a.end_min),
        "pilot_id": a.pilot_id,
        "boat_id": a.boat_id,
        "occupancies": [
            {
                "task_id": o.task_id,
                "segment_code": o.segment_code,
                "direction": o.direction,
                "entry_min": o.entry_min,
                "exit_min": o.exit_min,
                "entry": iso(o.entry_min),
                "exit": iso(o.exit_min),
            }
            for o in a.occupancies
        ],
    }


def _assignment_from_dict(d: dict) -> Assignment:
    from .domain import StepOccupancy

    return Assignment(
        task_id=d["task_id"],
        start_min=d["start_min"],
        end_min=d["end_min"],
        pilot_id=d["pilot_id"],
        boat_id=d.get("boat_id"),
        occupancies=tuple(
            StepOccupancy(
                task_id=o["task_id"],
                segment_code=o["segment_code"],
                direction=int(o["direction"]),
                entry_min=o["entry_min"],
                exit_min=o["exit_min"],
            )
            for o in d["occupancies"]
        ),
    )


def _relocations_to_dict(table: RelocationTable) -> dict:
    return {
        "default_min": table.default_min,
        "travel": [
            {"origin": a, "destination": b, "minutes": m}
            for (a, b), m in sorted(table.travel_min.items())
        ],
    }


def _relocations_from_dict(d: dict) -> RelocationTable:
    return RelocationTable(
        travel_min={
            (item["origin"], item["destination"]): item["minutes"]
            for item in d.get("travel", [])
        },
        default_min=d.get("default_min"),
    )


# ---------------------------------------------------------------------------
# 计划与修订
# ---------------------------------------------------------------------------
@dataclass
class Plan:
    """已锁定的计划版本（不可变，含完整规则快照）。"""

    plan_id: str
    revision_no: int
    note: str
    topology_snapshot: TopologySnapshot
    schedule: Schedule
    tide_windows: tuple[TideWindow, ...]
    pilots: tuple[Pilot, ...]
    boats: tuple[Boat, ...]
    tasks: tuple[PilotageTask, ...]
    pilot_relocations: RelocationTable
    boat_relocations: RelocationTable

    def get_assignment(self, task_id: str) -> Assignment:
        return self.schedule.get(task_id)

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "plan_id": self.plan_id,
            "revision_no": self.revision_no,
            "note": self.note,
            "topology": {
                "segments": {
                    code: _segment_to_dict(seg)
                    for code, seg in sorted(self.topology_snapshot.segments.items())
                },
                "routes": {
                    name: _route_to_dict(route)
                    for name, route in sorted(self.topology_snapshot.routes.items())
                },
            },
            "rules": {
                "tide_windows": [
                    {
                        "open": iso(w.open_min),
                        "close": iso(w.close_min),
                        "label": w.label,
                    }
                    for w in self.tide_windows
                ],
                "pilots": [
                    {"id": p.id, "name": p.name, "grade": p.grade}
                    for p in self.pilots
                ],
                "boats": [
                    {
                        "id": b.id,
                        "name": b.name,
                        "lead_min": b.lead_min,
                        "tail_min": b.tail_min,
                    }
                    for b in self.boats
                ],
                "pilot_relocations": _relocations_to_dict(self.pilot_relocations),
                "boat_relocations": _relocations_to_dict(self.boat_relocations),
            },
            "tasks": [_task_to_dict(t) for t in self.tasks],
            "assignments": [
                _assignment_to_dict(a)
                for _, a in sorted(self.schedule.assignments.items())
            ],
        }


@dataclass
class RevisionSpec:
    """显式修订内容。任何字段为 None 表示沿用旧版本快照中的值。"""

    note: str = ""
    add_tasks: list[PilotageTask] = field(default_factory=list)
    remove_task_ids: list[str] = field(default_factory=list)
    modify_tasks: list[PilotageTask] = field(default_factory=list)
    replace_segments: list[Segment] = field(default_factory=list)
    add_segments: list[Segment] = field(default_factory=list)
    tide_windows: Optional[list[TideWindow]] = None
    pilots: Optional[list[Pilot]] = None
    boats: Optional[list[Boat]] = None
    pilot_relocations: Optional[RelocationTable] = None
    boat_relocations: Optional[RelocationTable] = None
    topology: Optional[Topology | TopologySnapshot] = None
    """整体替换拓扑（最高优先级，含新航段/新路由/新规则）。"""


def _snapshot_from_plan_dict(d: dict) -> TopologySnapshot:
    topo = d["topology"]
    return TopologySnapshot(
        segments={
            code: _segment_from_dict(seg_d)
            for code, seg_d in topo["segments"].items()
        },
        routes={
            name: _route_from_dict(r)
            for name, r in topo.get("routes", {}).items()
        },
    )


def _plan_from_dict(d: dict) -> Plan:
    rules = d["rules"]
    assignments = [_assignment_from_dict(a) for a in d["assignments"]]
    return Plan(
        plan_id=d["plan_id"],
        revision_no=d["revision_no"],
        note=d.get("note", ""),
        topology_snapshot=_snapshot_from_plan_dict(d),
        schedule=Schedule(assignments={a.task_id: a for a in assignments}),
        tide_windows=tuple(
            TideWindow(
                open_min=to_min(parse_iso(w["open"])),
                close_min=to_min(parse_iso(w["close"])),
                label=w.get("label", ""),
            )
            for w in rules["tide_windows"]
        ),
        pilots=tuple(
            Pilot(id=p["id"], name=p["name"], grade=p["grade"])
            for p in rules["pilots"]
        ),
        boats=tuple(
            Boat(
                id=b["id"],
                name=b["name"],
                lead_min=b.get("lead_min", 0),
                tail_min=b.get("tail_min", 0),
            )
            for b in rules["boats"]
        ),
        tasks=tuple(_task_from_dict(t) for t in d["tasks"]),
        pilot_relocations=_relocations_from_dict(rules.get(
            "pilot_relocations", {}
        )),
        boat_relocations=_relocations_from_dict(rules.get(
            "boat_relocations", {}
        )),
    )


def _build_problem(plan: Plan) -> Problem:
    return Problem(
        topology=plan.topology_snapshot,
        tasks=list(plan.tasks),
        tide_windows=list(plan.tide_windows),
        pilots=list(plan.pilots),
        boats=list(plan.boats),
        pilot_relocations=plan.pilot_relocations,
        boat_relocations=plan.boat_relocations,
    )


def _apply_revision(plan: Plan, spec: RevisionSpec,
                    new_revision_no: int) -> Plan:
    """在内存中基于旧快照生成候选新计划（尚未求解/落盘）。"""
    if spec.topology is not None:
        topo = spec.topology
        snapshot = (
            topo.snapshot() if isinstance(topo, Topology) else topo
        )
    else:
        segments = dict(plan.topology_snapshot.segments)
        routes = dict(plan.topology_snapshot.routes)
        for seg in spec.add_segments + spec.replace_segments:
            segments[seg.code] = seg
        snapshot = TopologySnapshot(segments=segments, routes=routes)

    tasks = {t.id: t for t in plan.tasks}
    for tid in spec.remove_task_ids:
        tasks.pop(tid, None)
    for t in spec.add_tasks + spec.modify_tasks:
        tasks[t.id] = t

    new_plan = Plan(
        plan_id=plan.plan_id,
        revision_no=new_revision_no,
        note=spec.note,
        topology_snapshot=snapshot,
        schedule=Schedule(assignments={}),
        tide_windows=tuple(spec.tide_windows)
        if spec.tide_windows is not None
        else plan.tide_windows,
        pilots=tuple(spec.pilots)
        if spec.pilots is not None
        else plan.pilots,
        boats=tuple(spec.boats)
        if spec.boats is not None
        else plan.boats,
        tasks=tuple(tasks.values()),
        pilot_relocations=spec.pilot_relocations
        if spec.pilot_relocations is not None
        else plan.pilot_relocations,
        boat_relocations=spec.boat_relocations
        if spec.boat_relocations is not None
        else plan.boat_relocations,
    )
    return new_plan


# ---------------------------------------------------------------------------
# 计划仓库（原子落盘）
# ---------------------------------------------------------------------------
class PlanStore:
    """基于目录的计划版本仓库。

    目录结构::

        <dir>/index.json          # 所有版本号清单
        <dir>/lock.json           # 当前锁定版本指针
        <dir>/plans/v1.json ...   # 不可变版本文件
    """

    def __init__(self, directory: str):
        self.directory = directory
        os.makedirs(os.path.join(directory, _PLANS_DIR), exist_ok=True)
        if not os.path.exists(os.path.join(directory, _INDEX_FILE)):
            self._atomic_write_json(_INDEX_FILE, {"plan_id": None, "revisions": []})

    # ---- 锁定 ----
    def lock_plan(
        self,
        plan_id: str,
        problem: Problem,
        note: str = "initial lock",
    ) -> Plan | Infeasible:
        """求解并把首个锁定版本（v1）原子落盘。"""
        result = solve(problem)
        if isinstance(result, Infeasible):
            return result

        topology = problem.topology
        snapshot = topology.snapshot() if isinstance(topology, Topology) else topology
        plan = Plan(
            plan_id=plan_id,
            revision_no=1,
            note=note,
            topology_snapshot=snapshot,
            schedule=result,
            tide_windows=tuple(problem.tide_windows),
            pilots=tuple(problem.pilots),
            boats=tuple(problem.boats),
            tasks=tuple(problem.tasks),
            pilot_relocations=problem.pilot_relocations,
            boat_relocations=problem.boat_relocations,
        )
        self._commit(plan)
        return plan

    # ---- 显式修订 ----
    def revise(
        self, revision_no: Optional[int], spec: RevisionSpec
    ) -> Plan | Infeasible:
        """基于指定版本（默认当前锁定版本）做显式修订。

        修订失败（无解）时不写任何版本文件，旧版本仍可查询。
        """
        base = self.get_plan(revision_no) if revision_no else self.get_locked()
        if base is None:
            raise LookupError("没有可修订的锁定计划")

        # 版本号全局单调递增：即使基于旧版本分叉修订，也不会覆盖/冲撞
        # 已经存在的版本文件。
        existing = self.list_revisions()
        new_revision_no = (max(existing) + 1) if existing else 1
        candidate = _apply_revision(base, spec, new_revision_no)
        result = solve(_build_problem(candidate))
        if isinstance(result, Infeasible):
            # 关键：失败直接返回，不落盘任何半成品
            return result
        candidate.schedule = result
        self._commit(candidate)
        return candidate

    # ---- 查询（旧版本始终可查） ----
    def get_plan(self, revision_no: int) -> Plan:
        path = self._version_path(revision_no)
        if not os.path.exists(path):
            raise LookupError(f"版本 v{revision_no} 不存在")
        with open(path, "r", encoding="utf-8") as fh:
            return _plan_from_dict(json.load(fh))

    def get_locked(self) -> Optional[Plan]:
        lock = self._read_lock()
        if lock is None:
            return None
        return self.get_plan(lock["revision_no"])

    def get_locked_version(self) -> Optional[int]:
        lock = self._read_lock()
        return None if lock is None else lock["revision_no"]

    def list_revisions(self) -> list[int]:
        with open(
            os.path.join(self.directory, _INDEX_FILE), "r", encoding="utf-8"
        ) as fh:
            return list(json.load(fh)["revisions"])

    # ---- 原子落盘 ----
    def _commit(self, plan: Plan) -> None:
        version_path = self._version_path(plan.revision_no)
        if os.path.exists(version_path):
            raise RuntimeError(
                f"版本 v{plan.revision_no} 已存在且不可变，拒绝覆盖"
            )

        index_path = os.path.join(self.directory, _INDEX_FILE)
        with open(index_path, "r", encoding="utf-8") as fh:
            index = json.load(fh)

        revisions = list(index.get("revisions", []))
        revisions.append(plan.revision_no)
        new_index = {"plan_id": plan.plan_id, "revisions": revisions}
        new_lock = {
            "plan_id": plan.plan_id,
            "revision_no": plan.revision_no,
        }

        # 1) 新版本先完整写到临时文件并落盘；2) 再切清单；3) 最后切锁指针。
        # 任一步失败，锁指针都仍指向上一版本，不会出现半成品被锁定。
        tmp_plan = version_path + ".tmp"
        self._atomic_write_json(
            os.path.relpath(version_path, self.directory),
            plan.to_dict(),
            temp_name=os.path.basename(tmp_plan),
        )
        self._atomic_write_json(_INDEX_FILE, new_index)
        self._atomic_write_json(_LOCK_FILE, new_lock)

    def _version_path(self, revision_no: int) -> str:
        return os.path.join(
            self.directory, _PLANS_DIR, f"v{revision_no}.json"
        )

    def _read_lock(self) -> Optional[dict]:
        path = os.path.join(self.directory, _LOCK_FILE)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def _atomic_write_json(
        self, relative_path: str, payload: dict, *, temp_name: Optional[str] = None
    ) -> None:
        target = os.path.join(self.directory, relative_path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        name = temp_name or (".tmp-" + os.path.basename(target))
        fd, tmp_path = tempfile.mkstemp(
            dir=os.path.dirname(target), prefix=name
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, target)
        except BaseException:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise
