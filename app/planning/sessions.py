"""会话状态（：内存实现，M3 落 SQLite 持久化）。

进程重启即失，仅存演示会话的轻量上下文：已取消订阅、最近意图、最近确认。
业务数据永远以数据库为准，这里只存"对话衔接用"的指针。
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone

_TZ8 = timezone(timedelta(hours=8))
_LOCK = threading.Lock()
_SESSIONS: dict[str, dict] = {}


def now8() -> datetime:
    return datetime.now(_TZ8)


def new_session_id() -> str:
    return f"s-{uuid.uuid4().hex[:12]}"


def get_session(session_id: str) -> dict:
    with _LOCK:
        return _SESSIONS.setdefault(
            session_id,
            {"created_at": now8().isoformat(), "cancelled_sub_ids": [],
             "last_intent": None, "last_pending_id": None},
        )


def reset_session(session_id: str) -> None:
    with _LOCK:
        _SESSIONS.pop(session_id, None)
