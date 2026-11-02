"""模拟时钟：固定模拟当前日，保证数据与演示可复现。
接入真实环境时唯一改动点在这里。
"""
from __future__ import annotations

from datetime import date

SIM_TODAY = date(2026, 10, 14)


def sim_today() -> date:
    return SIM_TODAY
