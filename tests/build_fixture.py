# -*- coding: utf-8 -*-
"""生成测试夹具：从 transfer_confirm.csv（转账确认样本集，1958 条）按状态采样子集。

来源说明：
- 数据集只作测试夹具/语料，不替换演示 seed；引用时标注数据源；
- 本脚本一次性运行生成 JSON 夹具，冒烟脚本只读夹具。
"""
import csv
import json
import os
import sys
from decimal import Decimal

SRC = os.path.join(os.path.dirname(__file__), "fixtures", "transfer_confirm.csv")
OUT = os.path.join(os.path.dirname(__file__), "fixtures", "transfer_confirm_cases.json")

rows = []
with open(SRC, encoding="utf-8-sig") as f:
    for r in csv.DictReader(f):
        rows.append(r)

status_counts: dict[str, int] = {}
for r in rows:
    status_counts[r["confirm_status"]] = status_counts.get(r["confirm_status"], 0) + 1


def cents(r) -> int:
    return int((Decimal(r["amount"]) * 100).to_integral_value())


def ttl_minutes(r) -> float:
    from datetime import datetime
    fmt = "%Y-%m-%d %H:%M:%S.%f"
    return round((datetime.strptime(r["expire_time"], fmt)
                  - datetime.strptime(r["create_time"], fmt)).total_seconds() / 60, 1)


def pick(samples, n):
    return samples[:n]


expired = [r for r in rows if r["confirm_status"] == "已过期"]
rejected = [r for r in rows if r["confirm_status"] == "用户拒绝"]
confirmed = [r for r in rows if r["confirm_status"] == "已确认"]

case = {
    "source": "转账确认样本集 transfer_confirm.csv（覆盖已确认 / 拒绝 / 过期三类状态）",
    "total": len(rows),
    "status_counts": status_counts,
    "amount_line_note": "数据集口径：金额≥500 元强制 L2；本项目口径：所有转账一律 L2（更保守，方向安全）",
    "ttl_note": "数据集 TTL=5 分钟；本项目 TTL=30 分钟（TTL_DEFAULT_MIN）。过期语义一致：过期后表态 → CONFIRMATION_EXPIRED",
    "cases": {
        "expired": [{"confirm_id": r["confirm_id"], "amount": r["amount"],
                     "amount_cents": cents(r), "action": r["action"],
                     "ttl_minutes": ttl_minutes(r)} for r in pick(expired, 10)],
        "rejected": [{"confirm_id": r["confirm_id"], "amount": r["amount"],
                      "amount_cents": cents(r), "action": r["action"]} for r in pick(rejected, 10)],
        "confirmed_small": [{"confirm_id": r["confirm_id"], "amount": r["amount"],
                             "amount_cents": cents(r), "action": r["action"]}
                            for r in pick([x for x in confirmed if cents(x) <= 60000], 3)],
    },
}

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(case, f, ensure_ascii=False, indent=1)

print(f"total={case['total']} status={case['status_counts']}")
print(f"expired={len(case['cases']['expired'])} rejected={len(case['cases']['rejected'])} "
      f"confirmed_small={len(case['cases']['confirmed_small'])}")
sys.exit(0)
