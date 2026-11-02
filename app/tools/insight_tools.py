"""F4 洞察类工具：AA 分摊（T16）——确定性、可复算、合计严格一致（N18/N19）。"""
from __future__ import annotations

from app.tools.registry import l0_tool, result


@l0_tool("insights.aa_split")
def insights_aa_split(total_cents: int, people: int) -> dict:
    if not isinstance(total_cents, int) or isinstance(total_cents, bool) or total_cents <= 0:
        raise ValueError("总额必须为正整数（分）")
    if not isinstance(people, int) or isinstance(people, bool) or people <= 0:
        raise ValueError("人数必须为正整数")
    base = total_cents // people
    rem = total_cents % people
    shares = [{"person_no": i + 1,
               "share_cents": base + 1 if i < rem else base}
              for i in range(people)]
    assert sum(s["share_cents"] for s in shares) == total_cents, "分摊合计与总额不一致"
    return result("insights.aa_split",
                  {"total_cents": total_cents, "people": people}, {
                      "total_cents": total_cents, "people": people,
                      "shares": shares,
                      "disclaimer": "本次为分摊单，不代表已收款；非发起人显示『待收款』。",
                  })
