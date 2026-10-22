"""Small display helpers shared by segments, offers and the bot."""
from __future__ import annotations


def indian_number(x: float) -> str:
    """Indian digit grouping: 817139 -> '8,17,139'."""
    s = f"{abs(round(x)):d}"
    head, tail = s[:-3], s[-3:]
    while len(head) > 2:
        tail = f"{head[-2:]},{tail}"
        head = head[:-2]
    return ("-" if x < 0 else "") + (f"{head},{tail}" if head else tail)


def plain_number(x: float) -> str:
    """15.0 -> '15', 12.5 -> '12.5'."""
    return f"{x:g}"
