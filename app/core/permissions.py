"""三级权限模型。

- L0 只读：先执行后汇报
- L1 低风险写（取消订阅/改提醒）：说明影响 → 用户确认 → 执行 → 凭证
- L2 资金操作（转账/定时转账/取消资金计划）：强制确认 → 执行 → 真实凭证

权限状态由代码管理：模型自报等级无效；对话文本中
「用户已授权」「开发者模式」等声明一律不作为授权证据。
确认绑定：用户 + 操作 + 规范化参数 + 当前有效状态，参数变化即失效。
"""
from __future__ import annotations

from enum import IntEnum


class PermissionLevel(IntEnum):
    L0 = 0  # 只读
    L1 = 1  # 低风险写操作
    L2 = 2  # 资金操作


# 工具白名单：工具名 → 权限等级
TOOL_PERMISSIONS: dict[str, PermissionLevel] = {}


def register_tool(name: str, level: PermissionLevel) -> None:
    """注册工具及其权限等级。重复注册不同等级视为配置错误。"""
    existing = TOOL_PERMISSIONS.get(name)
    if existing is not None and existing != level:
        raise ValueError(f"工具 {name} 权限等级冲突: {existing} vs {level}")
    TOOL_PERMISSIONS[name] = level


def get_tool_level(name: str) -> PermissionLevel | None:
    """白名单外工具返回 None —— 调用方必须拒绝（模型不得自报等级）。"""
    return TOOL_PERMISSIONS.get(name)
