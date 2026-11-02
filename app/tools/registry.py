"""工具注册与执行入口。

- 白名单唯一权威：TOOL_PERMISSIONS（app/core/permissions.py），模型自报等级无效。
- 每个工具返回统一结构：{tool, params, data, source, error?}；L0 查询 source 恒为 "tool"。
"""
from __future__ import annotations

import json
import re
from functools import wraps
from typing import Any, Callable

from app.core.business_policy import require_write_enabled

from app.core.logging import get_logger
from app.core.permissions import PermissionLevel, get_tool_level, register_tool

logger = get_logger("app.tools")

TOOL_IMPLS: dict[str, Callable[..., dict]] = {}

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_YEAR_RE = re.compile(r"^\d{4}$")


def l0_tool(name: str) -> Callable:
    """L0 只读工具注册装饰器。"""

    def deco(fn: Callable[..., dict]) -> Callable[..., dict]:
        register_tool(name, PermissionLevel.L0)
        TOOL_IMPLS[name] = fn
        return fn

    return deco


def l1_tool(name: str) -> Callable:
    """L1 低风险写操作注册装饰器。执行必须经确认服务（approve 事件）触发。"""

    def deco(fn: Callable[..., dict]) -> Callable[..., dict]:
        @wraps(fn)
        def guarded(*args, **kwargs):
            require_write_enabled(name)
            return fn(*args, **kwargs)
        register_tool(name, PermissionLevel.L1)
        TOOL_IMPLS[name] = guarded
        return guarded

    return deco


def l2_tool(name: str) -> Callable:
    """L2 资金操作注册装饰器。执行必须经确认服务（approve 事件）触发。"""

    def deco(fn: Callable[..., dict]) -> Callable[..., dict]:
        @wraps(fn)
        def guarded(*args, **kwargs):
            require_write_enabled(name)
            return fn(*args, **kwargs)
        register_tool(name, PermissionLevel.L2)
        TOOL_IMPLS[name] = guarded
        return guarded

    return deco


def validate_month(month: str) -> str:
    if not _MONTH_RE.match(month or ""):
        raise ValueError("月份格式应为 YYYY-MM")
    return month


def validate_year(year: str) -> str:
    if not _YEAR_RE.match(year or ""):
        raise ValueError("年份格式应为 YYYY")
    return year


def result(tool: str, params: dict, data: Any) -> dict:
    return {"tool": tool, "params": params, "data": data, "source": "tool"}


def dump_params(params: dict) -> str:
    return json.dumps(params, ensure_ascii=False, sort_keys=True)
