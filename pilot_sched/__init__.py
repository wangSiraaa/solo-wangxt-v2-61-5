"""离线引航排班系统：虚构航道拓扑 + 统一约束排班模型。

约束维度（全部进入同一个排班模型）：
  1. 潮窗（tide windows）—— 任务只能在允许的起始时间窗内开始
  2. 人员（pilots）—— 同一引航员任务区间不得重叠，任务间需满足转场时间
  3. 接送艇（boats）—— 同一艘艇任务区间不得重叠，任务间需满足转场时间
  4. 转场（transfers）—— 上一任务结束地点到下一任务开始地点的路程时间
  5. 航道（waterway）—— 同段反向会遇禁止；同向必须满足 headway；不同航段可并行
"""

from .timeutil import TimeKeeper, parse_clock
from .topology import Direction, Segment, WaterwayTopology, SafetyRules
from .tasks import RouteLeg, PilotageTask, Pilot, Boat
from .scheduler import Scheduler, Assignment, Conflict, SchedulingResult
from .plan import LockedPlan, PlanRevision, PlanStore, RevisionError, PlanNotFound

__all__ = [
    "TimeKeeper",
    "parse_clock",
    "Direction",
    "Segment",
    "WaterwayTopology",
    "SafetyRules",
    "RouteLeg",
    "PilotageTask",
    "Pilot",
    "Boat",
    "Scheduler",
    "Assignment",
    "Conflict",
    "SchedulingResult",
    "LockedPlan",
    "PlanRevision",
    "PlanStore",
    "RevisionError",
    "PlanNotFound",
]
