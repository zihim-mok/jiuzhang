"""业务写策略开关：三档业务写权限，由环境变量 BUSINESS_WRITES 选择。

- all（默认）  ：全部写能力开放——退订（L1）、转账与定时任务（L2），
                 一律走「创建确认单 → 用户确认 → 执行」的既有流程。
- cancel_only ：仅放行订阅退订（F2.SUB_CANCEL / subscriptions.cancel），
                 转账与定时任务等 L2 资金操作仍被拒绝。
- readonly    ：全部业务写拒绝（含确认单创建），只提供查询与分析。

模式在进程启动时读取一次，切换需重启服务；运行中不接受任何调用方
（模型 / 前端 / 请求参数 / 对话文本）声明的模式变更。
会话/消息记录以及服务日志不属于金融业务写入；它们在任何模式下都可持久化。
"""
from __future__ import annotations

import os

_RAW_MODE = (os.getenv("BUSINESS_WRITES") or "all").strip().lower()
WRITES_MODE: str = _RAW_MODE if _RAW_MODE in ("all", "cancel_only", "readonly") else "all"

# 全部业务写意图（只读档全禁；cancel_only 档放行退订）
_ALL_WRITE_INTENTS = frozenset({
    "F3.TRANSFER", "F3.SCHED_CREATE", "F3.SCHED_CANCEL", "F2.SUB_CANCEL"
})

DENIED_INTENTS: frozenset[str] = (
    frozenset() if WRITES_MODE == "all"
    else frozenset(_ALL_WRITE_INTENTS - ({"F2.SUB_CANCEL"} if WRITES_MODE == "cancel_only" else set()))
)

# 可放行的写动作（与 confirmation_service.ACTION_LEVELS 同源的动作名）
_ENABLED_ACTIONS: dict[str, frozenset[str]] = {
    "all": frozenset({
        "subscriptions.cancel", "transfers.create",
        "transfers.scheduled.create", "transfers.scheduled.cancel",
    }),
    "cancel_only": frozenset({"subscriptions.cancel"}),
    "readonly": frozenset(),
}


class BusinessReadOnlyError(PermissionError):
    code = "BUSINESS_READ_ONLY"

    def __init__(self, message: str | None = None):
        super().__init__(message or (
            "当前为业务只读模式：不允许转账、取消订阅、创建或取消定时任务，也不生成确认单。"
            "（可配置 BUSINESS_WRITES=all 恢复全部写能力）"))


def action_write_enabled(action: str) -> bool:
    """按动作粒度判断写是否放行（服务端权威，不信任调用方自报）。"""
    return action in _ENABLED_ACTIONS[WRITES_MODE]


def require_write_enabled(action: str | None = None) -> None:
    """确定性写执行器在进入数据变更前必须调用。

    - 传 action：按动作粒度判断（L1/L2 注册工具、确认服务等）。
    - 不传 action：底层资金执行等无动作上下文的入口，仅在 all 档放行。
    """
    if action is not None:
        if not action_write_enabled(action):
            raise BusinessReadOnlyError()
        return
    if WRITES_MODE != "all":
        raise BusinessReadOnlyError()
