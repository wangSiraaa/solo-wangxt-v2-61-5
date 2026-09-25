"""时间工具：以“自锚点起的绝对分钟数”为内部表示。

跨午夜（甚至跨多日）只是更大的整数，因此 23:50 开始、时长 40 分钟的航段
会自然占用到次日 00:30，无需任何特殊的翻日处理。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable, Optional

# 全局锚点（仅用于绝对分钟 <-> datetime 的换算，业务判断不依赖具体取值）
EPOCH: datetime = datetime(2020, 1, 1)
MINUTE: timedelta = timedelta(minutes=1)


def to_min(dt: datetime) -> int:
    """datetime -> 自锚点起的绝对分钟数（整数，忽略秒以下部分）。"""
    return int((dt.replace(second=0, microsecond=0) - EPOCH) / MINUTE)


def to_dt(minutes: int) -> datetime:
    """绝对分钟数 -> datetime。"""
    return EPOCH + timedelta(minutes=minutes)


def iso(minutes: int) -> str:
    """绝对分钟数 -> ISO 字符串（用于冲突响应/快照序列化）。"""
    return to_dt(minutes).isoformat()


def parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(text)


def intervals_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """半开区间 [start, end) 是否重叠（端点相接不算重叠）。"""
    return a_start < b_end and b_start < a_end


def gap_between(earlier_end: int, later_start: int) -> int:
    """两个先后区间的间隔分钟数（后者在前则为负，表示重叠/倒挂）。"""
    return later_start - earlier_end


def clock_label(minutes: int) -> str:
    """仅用于日志/报错的 HH:MM 标签（可跨日）。"""
    return to_dt(minutes).strftime("%H:%M")


def merge_intervals(intervals: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """合并重叠或相接的半开区间。"""
    ordered = sorted(intervals)
    merged: list[tuple[int, int]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def fmt_duration(minutes: Optional[int]) -> str:
    if minutes is None:
        return "-"
    return f"{minutes}min"
