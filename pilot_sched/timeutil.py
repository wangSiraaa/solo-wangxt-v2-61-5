"""时间工具：以“基准日午夜起的绝对分钟数”表示时间，天然支持跨午夜航段。

例如次日 01:00 记为 1500；航段占用区间可能跨过 1440 分钟边界，
所有约束计算都在绝对分钟轴上进行，不做取模，因此跨午夜占用被正确保留。
"""

from __future__ import annotations


def parse_clock(text: str) -> int:
    """把 ``"HH:MM"`` 解析为当天分钟数（0..1439）。"""
    text = text.strip()
    parts = text.split(":")
    if len(parts) != 2:
        raise ValueError(f"非法时间格式: {text!r}，应为 HH:MM")
    hh, mm = int(parts[0]), int(parts[1])
    if not (0 <= hh <= 47 and 0 <= mm <= 59):
        raise ValueError(f"非法时间: {text!r}")
    return hh * 60 + mm


def format_clock(minutes: int) -> str:
    """绝对分钟数格式化为 ``[d+]HH:MM``：``d+`` 表示基准日之后第 d 天。

    例如 1500 -> ``"1+01:00"``（次日凌晨 1 点），780 -> ``"13:00"``。
    """
    day, rem = divmod(int(minutes), 1440)
    prefix = f"{day}+" if day else ""
    return f"{prefix}{rem // 60:02d}:{rem % 60:02d}"


class TimeKeeper:
    """基准日历与时间轴。

    模型内部一律使用绝对分钟偏移；潮窗等以时钟给出的输入在此绑定到具体基准日，
    允许潮窗/航段跨越午夜。
    """

    def __init__(self, base_date: str = "2026-09-24"):
        self.base_date = base_date

    def at(self, clock: str, day_offset: int = 0) -> int:
        """``"HH:MM"`` + 天数偏移 -> 绝对分钟。"""
        return parse_clock(clock) + day_offset * 1440

    def window(self, start_clock: str, end_clock: str,
               end_day_offset: int | None = None) -> tuple[int, int]:
        """构造时间窗。结束时钟若早于开始时钟，则自动视为跨到次日；
        也可通过 ``end_day_offset`` 显式指定结束所在的天。"""
        start = self.at(start_clock)
        if end_day_offset is None:
            end = parse_clock(end_clock)
            if end <= start:
                end += 1440
        else:
            end = self.at(end_clock, end_day_offset)
        return start, end

    def fmt(self, minutes: int) -> str:
        return format_clock(minutes)

    def fmt_range(self, start: int, end: int) -> str:
        return f"[{format_clock(start)}, {format_clock(end)})"
