"""LangGraph 执行调度——把确定性规划挂上状态图。

图结构：
    sync（会话确认状态同步）→ classify（意图理解）─→ dispatch（任务分解与执行）→ END
                                              └──→ fallback（超范围标准应答）→ END

- 节点复用 stub_planner 的确定性处理器，行为与意图路由表一致；
- `_classify` 是意图理解唯一入口：替身用触发词，真模型接入后仅替换该节点内部实现，
  同一张意图表、同一套块契约；
- dispatch 内置异常兜底：如实报错、不补造结果。
"""
from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, StateGraph

from app.planning import stub_planner as SP
from app.planning.sessions import get_session


class ChatState(TypedDict, total=False):
    """/chat 一次请求的状态载体。"""
    session_id: str
    message: str
    intent: str
    blocks: list
    tool_results: list
    pending: dict | None


def _node_sync(state: ChatState) -> dict:
    """把已生效的确认结果同步进会话（SAVINGS 等后续节点要读）。"""
    session = get_session(state["session_id"])
    SP._sync_session(session)
    return {}


def _node_classify(state: ChatState) -> dict:
    """意图理解：槽位补答优先 → 规则触发词；规则落 FALLBACK 时才问真模型（只定意图）。"""
    session = get_session(state["session_id"])
    t = state["message"].strip()
    intent, source, reason = SP.classify_intent(
        t, session.get("pending_slots"), session.get("history"))
    session["last_intent_source"] = source
    # 仅故障时携带原因，供兜底节点区分「模型不可用」与「超出范围」
    if reason:
        session["last_model_reason"] = reason
    else:
        session.pop("last_model_reason", None)
    return {"intent": intent}


def _node_dispatch(state: ChatState) -> dict:
    """任务分解与执行：意图 → 工具序列 → 块结构；异常由 _dispatch 兜底。"""
    session = get_session(state["session_id"])
    t = state["message"].strip()
    intent = state["intent"]
    blks, trs, pending = SP._dispatch(session, t, intent)
    session["last_intent"] = intent
    return {"blocks": blks, "tool_results": trs, "pending": pending}


def _node_fallback(state: ChatState) -> dict:
    """超范围标准应答：表外输入清除槽位 + 能力清单，不猜测不硬撑。"""
    session = get_session(state["session_id"])
    blks, trs, pending = SP._h_fallback(session, state["message"].strip())
    session["last_intent"] = "F4.FALLBACK"
    return {"blocks": blks, "tool_results": trs, "pending": pending}


def _edge_after_classify(state: ChatState) -> str:
    return "dispatch" if state["intent"] != "F4.FALLBACK" else "fallback"


_builder = StateGraph(ChatState)
_builder.add_node("sync", _node_sync)
_builder.add_node("classify", _node_classify)
_builder.add_node("dispatch", _node_dispatch)
_builder.add_node("fallback", _node_fallback)
_builder.set_entry_point("sync")
_builder.add_edge("sync", "classify")
_builder.add_conditional_edges(
    "classify", _edge_after_classify,
    {"dispatch": "dispatch", "fallback": "fallback"})
_builder.add_edge("dispatch", END)
_builder.add_edge("fallback", END)
_compiled = _builder.compile()


def run_graph(session_id: str, message: str) -> dict:
    """执行状态图，返回 /chat 响应体（不含 environment）。出口过数字/来源校验。

    meta 为接口契约的非破坏性增补：披露「意图由规则还是真模型判定」，
    供前端标注与验收留痕；不含任何密钥、金额或用户原文。
    """
    from app.core.config import get_config
    from app.planning.validator import validate_response
    out = _compiled.invoke({"session_id": session_id, "message": message})
    session = get_session(session_id)
    from app.planning.response_ai import enhance_blocks
    blocks, reply_source = enhance_blocks(
        message, out.get("intent", ""), session.get("last_intent_source", "rule"),
        out["blocks"], out["tool_results"], out["pending"], session.get("history"))
    resp = {
        "blocks": blocks,
        "tool_results": out["tool_results"],
        "pending_confirmation": out["pending"],
        "meta": {
            "intent_id": out.get("intent"),
            "reply_source": reply_source,
            "intent_source": session.get("last_intent_source", "rule"),
            "model_mode": "stub" if get_config().model_stub_mode else "live",
            # 仅当模型链路故障时给出原因码（如 http_402），便于前端/验收区分
            "model_reason": (session.get("last_model_reason")
                             if session.get("last_intent_source") == "rule_after_model_fail"
                             else None),
        },
    }
    return validate_response(resp)
