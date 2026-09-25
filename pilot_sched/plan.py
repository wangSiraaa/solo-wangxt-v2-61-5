"""计划锁定 / 快照 / 显式修订。

锁定（lock）时把当时的航道拓扑、安全规则、转场表、任务定义与排班结果整体做
JSON 快照落盘；之后离线拓扑或规则的任何变化都不会影响旧版本，必须通过一次
显式修订（revise）生成新版本才生效。

事务性保证：
  * 所有写入先写临时文件再 os.replace 原子替换；
  * revise 在任何一步（快照重建、求解、写盘）失败时，磁盘上既有版本原样保留，
    绝不留下“半成品”版本。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .scheduler import (
    Scheduler, Assignment, SchedulingResult,
)
from .tasks import PilotageTask, Pilot, Boat, RouteLeg
from .topology import WaterwayTopology, SafetyRules, Direction


class PlanNotFound(Exception):
    """计划不存在。"""


class RevisionError(Exception):
    """修订被拒绝（不可行/非法）；旧版本保持不变。"""

    def __init__(self, message: str, conflicts: list[dict] | None = None):
        super().__init__(message)
        self.conflicts = conflicts or []


# --------------------------------------------------------------------------
# 序列化
# --------------------------------------------------------------------------

def _task_to_dict(task: PilotageTask) -> dict:
    return {
        "id": task.id,
        "legs": [
            {"segment": l.segment, "direction": l.direction.value,
             "duration": l.duration}
            for l in task.legs
        ],
        "tide_windows": [[lo, hi] for lo, hi in task.tide_windows],
        "start_location": task.start_location,
        "end_location": task.end_location,
        "pickup_minutes": task.pickup_minutes,
        "dropoff_minutes": task.dropoff_minutes,
        "boat_required": task.boat_required,
        "fixed_pilot": task.fixed_pilot,
        "fixed_boat": task.fixed_boat,
    }


def _task_from_dict(data: dict) -> PilotageTask:
    return PilotageTask(
        id=data["id"],
        legs=tuple(
            RouteLeg(segment=l["segment"],
                     direction=Direction(l["direction"]),
                     duration=l["duration"])
            for l in data["legs"]
        ),
        tide_windows=tuple((w[0], w[1]) for w in data["tide_windows"]),
        start_location=data["start_location"],
        end_location=data["end_location"],
        pickup_minutes=data.get("pickup_minutes", 0),
        dropoff_minutes=data.get("dropoff_minutes", 0),
        boat_required=data.get("boat_required", True),
        fixed_pilot=data.get("fixed_pilot"),
        fixed_boat=data.get("fixed_boat"),
    )


def _rules_to_dict(rules: SafetyRules) -> dict:
    return {
        "same_direction_headway": rules.same_direction_headway,
        "opposite_clearance": rules.opposite_clearance,
        "forbid_opposite": rules.forbid_opposite,
        "segment_headways": dict(rules.segment_headways),
    }


def _rules_from_dict(data: dict) -> SafetyRules:
    return SafetyRules(
        same_direction_headway=data["same_direction_headway"],
        opposite_clearance=data["opposite_clearance"],
        forbid_opposite=data.get("forbid_opposite", True),
        segment_headways=dict(data.get("segment_headways", {})),
    )


def _people_to_dict(pilots, boats) -> dict:
    return {
        "pilots": [
            {"id": p.id, "home": p.home,
             "available_from": p.available_from,
             "available_to": p.available_to}
            for p in pilots.values()
        ],
        "boats": [
            {"id": b.id, "home": b.home,
             "available_from": b.available_from,
             "available_to": b.available_to}
            for b in boats.values()
        ],
    }


def _people_from_dict(data: dict):
    pilots = {p["id"]: Pilot(**p) for p in data.get("pilots", [])}
    boats = {b["id"]: Boat(**b) for b in data.get("boats", [])}
    return pilots, boats


def _transfers_to_dict(transfer_minutes) -> list:
    out, seen = [], set()
    for (a, b), minutes in transfer_minutes.items():
        key = tuple(sorted((a, b)))
        if key in seen:
            continue
        seen.add(key)
        out.append({"from": key[0], "to": key[1], "minutes": minutes})
    return out


def _transfers_from_dict(data) -> dict:
    return {(e["from"], e["to"]): e["minutes"] for e in data}


# --------------------------------------------------------------------------
# 锁定计划
# --------------------------------------------------------------------------

@dataclass
class PlanRevision:
    """一次修订的元信息（显式变更，生成新版本）。"""

    version: int
    based_on_version: int
    note: str
    changes: dict
    created_at: str

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "based_on_version": self.based_on_version,
            "note": self.note,
            "changes": self.changes,
            "created_at": self.created_at,
        }


@dataclass
class LockedPlan:
    """锁定计划版本：内嵌完整的拓扑/规则/资源/任务/排班快照。"""

    plan_id: str
    version: int
    locked_at: str
    note: str
    topology_snapshot: dict
    rules_snapshot: dict
    transfers: list
    people_snapshot: dict
    tasks: dict[str, PilotageTask]
    assignments: dict[str, Assignment]
    feasible: bool
    revisions: list[PlanRevision] = field(default_factory=list)

    # -- 快照访问：锁定后拓扑/规则必须从快照重建，与在线对象隔离 --

    def snapshot_topology(self) -> WaterwayTopology:
        return WaterwayTopology.from_snapshot(self.topology_snapshot)

    def snapshot_rules(self) -> SafetyRules:
        return _rules_from_dict(self.rules_snapshot)

    def assignment_for(self, task_id: str) -> Assignment:
        return self.assignments[task_id]

    def to_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "version": self.version,
            "locked_at": self.locked_at,
            "note": self.note,
            "feasible": self.feasible,
            "topology": self.topology_snapshot,
            "rules": self.rules_snapshot,
            "transfers": self.transfers,
            "people": self.people_snapshot,
            "tasks": {tid: _task_to_dict(t) for tid, t in self.tasks.items()},
            "assignments": {
                tid: a.to_dict() for tid, a in self.assignments.items()
            },
            "revisions": [r.to_dict() for r in self.revisions],
        }


# --------------------------------------------------------------------------
# 存储
# --------------------------------------------------------------------------

class PlanStore:
    """版本化计划存储：每个 plan_id 一个目录，v1/v2/... 不可变 JSON 文件。"""

    def __init__(self, root_dir: str):
        self.root_dir = os.path.abspath(root_dir)
        os.makedirs(self.root_dir, exist_ok=True)

    def _dir(self, plan_id: str) -> str:
        return os.path.join(self.root_dir, plan_id)

    def _path(self, plan_id: str, version: int) -> str:
        return os.path.join(self._dir(plan_id), f"v{version}.json")

    def _atomic_write_json(self, path: str, payload: dict) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=os.path.dirname(path), prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ---- 查询 ----

    def versions(self, plan_id: str) -> list[int]:
        d = self._dir(plan_id)
        if not os.path.isdir(d):
            return []
        out = []
        for name in os.listdir(d):
            if name.startswith("v") and name.endswith(".json"):
                try:
                    out.append(int(name[1:-5]))
                except ValueError:
                    continue
        return sorted(out)

    def exists(self, plan_id: str) -> bool:
        return bool(self.versions(plan_id))

    def get(self, plan_id: str, version: int | None = None) -> LockedPlan:
        versions = self.versions(plan_id)
        if not versions:
            raise PlanNotFound(f"计划不存在: {plan_id}")
        if version is None:
            version = versions[-1]
        elif version not in versions:
            raise PlanNotFound(
                f"计划 {plan_id} 版本 v{version} 不存在；"
                f"现有版本: {['v' + str(v) for v in versions]}")
        with open(self._path(plan_id, version), "r", encoding="utf-8") as f:
            return self._from_dict(json.load(f))

    def get_all(self, plan_id: str) -> list[LockedPlan]:
        return [self.get(plan_id, v) for v in self.versions(plan_id)]

    # ---- 锁定 ----

    def lock(
        self,
        plan_id: str,
        tasks,
        scheduler: Scheduler,
        note: str = "",
    ) -> LockedPlan:
        """求解并锁定为 v1；若该计划已存在则报错（修订请用 revise）。"""
        if self.exists(plan_id):
            raise RevisionError(
                f"计划 {plan_id} 已锁定，请使用 revise() 显式修订")
        result = scheduler.solve(tasks)
        if not result.feasible:
            raise RevisionError(
                f"计划 {plan_id} 无可行排班，拒绝锁定",
                conflicts=[c.to_dict() for c in result.conflicts])
        return self._build_and_write(
            plan_id=plan_id,
            version=1,
            note=note,
            tasks={t.id: t for t in tasks},
            scheduler=scheduler,
            result=result,
            revisions=[],
            changes=None,
            based_on=0,
        )

    # ---- 显式修订 ----

    def revise(
        self,
        plan_id: str,
        changes: dict | None = None,
        note: str = "",
        *,
        tasks=None,
        topology: WaterwayTopology | None = None,
        rules: SafetyRules | None = None,
        pilots=None,
        boats=None,
        transfer_minutes=None,
        time_step: int | None = None,
    ) -> LockedPlan:
        """基于最新版本做一次显式修订。

        ``changes`` 可包含 "tasks"/"topology"/"rules"/"pilots"/"boats"/
        "transfer_minutes" 键；对应的具名参数等价。任何一步失败（无排班、
        非法变更、写盘错误）都不会产生新版本文件，旧版本仍可查询。
        """
        base = self.get(plan_id)  # 不存在会抛 PlanNotFound
        base_topo = topology or base.snapshot_topology()
        base_rules = rules or base.snapshot_rules()
        base_pilots, base_boats = _people_from_dict(base.people_snapshot)
        base_transfers = _transfers_from_dict(base.transfers)
        base_tasks = dict(base.tasks)

        change_record: dict = {"note": note}
        if changes:
            if "tasks" in changes:
                tasks = changes["tasks"]
            if "topology" in changes:
                base_topo = changes["topology"]
            if "rules" in changes:
                base_rules = changes["rules"]
            if "pilots" in changes:
                pilots = changes["pilots"]
            if "boats" in changes:
                boats = changes["boats"]
            if "transfer_minutes" in changes:
                transfer_minutes = changes["transfer_minutes"]
        if tasks is not None:
            new_tasks = {t.id: t for t in tasks}
            change_record["tasks"] = {
                "added": sorted(set(new_tasks) - set(base_tasks)),
                "removed": sorted(set(base_tasks) - set(new_tasks)),
                "modified": sorted(
                    tid for tid in set(new_tasks) & set(base_tasks)
                    if _task_to_dict(new_tasks[tid]) != _task_to_dict(base_tasks[tid])
                ),
            }
            base_tasks = new_tasks
        if pilots is not None:
            base_pilots = {p.id: p for p in pilots}
            change_record["pilots"] = sorted(base_pilots)
        if boats is not None:
            base_boats = {b.id: b for b in boats}
            change_record["boats"] = sorted(base_boats)
        if transfer_minutes is not None:
            base_transfers = dict(transfer_minutes)
            change_record["transfers_changed"] = True
        if topology is not None:
            change_record["topology_changed"] = True
        if rules is not None:
            change_record["rules_changed"] = _rules_to_dict(base_rules)

        new_scheduler = Scheduler(
            topology=base_topo,
            rules=base_rules,
            pilots=base_pilots.values(),
            boats=base_boats.values(),
            transfer_minutes=base_transfers,
            time_step=time_step or 1,
        )
        result = new_scheduler.solve(base_tasks.values())
        if not result.feasible:
            # 不写任何文件：调用方仍可 get() 到旧版本
            raise RevisionError(
                f"计划 {plan_id} 修订后无可行排班，修订被拒绝（旧版本 v"
                f"{base.version} 保持有效）",
                conflicts=[c.to_dict() for c in result.conflicts],
            )

        revisions = list(base.revisions) + [PlanRevision(
            version=base.version + 1,
            based_on_version=base.version,
            note=note,
            changes=change_record,
            created_at=datetime.now(timezone.utc).isoformat(),
        )]
        return self._build_and_write(
            plan_id=plan_id,
            version=base.version + 1,
            note=note,
            tasks=base_tasks,
            scheduler=new_scheduler,
            result=result,
            revisions=revisions,
            changes=change_record,
            based_on=base.version,
        )

    # ---- 内部 ----

    def _build_and_write(self, plan_id, version, note, tasks, scheduler,
                         result: SchedulingResult, revisions, changes,
                         based_on) -> LockedPlan:
        plan = LockedPlan(
            plan_id=plan_id,
            version=version,
            locked_at=datetime.now(timezone.utc).isoformat(),
            note=note,
            topology_snapshot=scheduler.topology.to_snapshot(),
            rules_snapshot=_rules_to_dict(scheduler.rules),
            transfers=_transfers_to_dict(scheduler.transfer_minutes),
            people_snapshot=_people_to_dict(scheduler.pilots, scheduler.boats),
            tasks=dict(tasks),
            assignments=dict(result.assignments),
            feasible=result.feasible,
            revisions=revisions,
        )
        payload = plan.to_dict()
        if changes is not None:
            payload["based_on_version"] = based_on
        # 原子写入；失败不会留下部分文件
        self._atomic_write_json(self._path(plan_id, version), payload)
        return plan

    def _from_dict(self, data: dict) -> LockedPlan:
        topology = WaterwayTopology.from_snapshot(data["topology"])  # noqa: F841
        return LockedPlan(
            plan_id=data["plan_id"],
            version=data["version"],
            locked_at=data["locked_at"],
            note=data.get("note", ""),
            topology_snapshot=data["topology"],
            rules_snapshot=data["rules"],
            transfers=data.get("transfers", []),
            people_snapshot=data.get("people", {"pilots": [], "boats": []}),
            tasks={tid: _task_from_dict(t) for tid, t in data["tasks"].items()},
            assignments={
                tid: Assignment(
                    task_id=a["task_id"], start=a["start"],
                    pilot_id=a["pilot_id"], boat_id=a.get("boat_id"))
                for tid, a in data["assignments"].items()
            },
            feasible=data.get("feasible", True),
            revisions=[
                PlanRevision(
                    version=r["version"],
                    based_on_version=r["based_on_version"],
                    note=r.get("note", ""),
                    changes=r.get("changes", {}),
                    created_at=r.get("created_at", ""),
                )
                for r in data.get("revisions", [])
            ],
        )
