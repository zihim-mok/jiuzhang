"""响应校验器——无来源阻断的代码层落点。

纪律来源（大纲 §4.1/§4.2、NFR-01/03）：
- 确认卡参数纪律：金额必须为正整数分（禁浮点/非正数入卡），非法卡受控阻断；
- 来源标记强制：响应中凡出现数据块（kv/table），必须伴随 source 块或工具结果，
  否则追加「缺少来源标记」警示——不静默放行（受控标注降级）；
- 防护不依赖模型提示词：本校验在图执行后对全部 /chat 响应生效（stub 与真模型同路径）。
"""
from __future__ import annotations

from app.planning import blocks as B


def validate_response(resp: dict) -> dict:
    """对 /chat 响应做数字与来源校验；非法确认卡受控阻断，缺来源的数据块追加警示。"""
    blocks = resp.get("blocks") or []
    tool_results = resp.get("tool_results") or []
    pending = resp.get("pending_confirmation")

    # 1) 确认卡参数纪律：amount_cents 一律正整数分；非法卡阻断为如实提示（A09 族）
    if pending:
        params = pending.get("normalized_params") or {}
        amount = params.get("amount_cents")
        bad_amount = amount is not None and (
            not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0)
        bad_level = pending.get("level") not in ("L1", "L2")
        if bad_amount or bad_level:
            return {
                "blocks": [B.text("该确认卡未通过服务端校验（金额或权限等级异常），已阻断执行。"
                                  "请重新发起操作；本次没有产生任何变更。"),
                           B.note("服务端数字防护：金额一律整数分、权限等级代码层校验。", "warn")],
                "tool_results": [],
                "pending_confirmation": None,
            }

    # 2) 数据数字必须有来源：kv/table 块出现时须伴随 source 块或工具结果（A09 族）
    has_data = any(b.get("type") in ("kv", "table") for b in blocks)
    has_source = any(b.get("type") == "source" for b in blocks)
    if has_data and not (has_source or tool_results):
        resp["blocks"] = blocks + [
            B.note("（服务端校验：以上数据缺少来源标记，请勿作为决策依据）", "warn")]
    return resp
