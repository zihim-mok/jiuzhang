"""F1 分类归一化。

依据：《数据集使用与F1规则规划_2026-10-18》§3.1——seed 类目名与本规则不一致处
在归一化层映射（居住/订阅→生活缴费等）；未知归入其他支出但保留标记。
"""
from __future__ import annotations

# 标准 9 类（【用户已确认】P-B2 规划基线）
STD_CATEGORIES = [
    "工资收入", "餐饮美食", "交通出行", "购物消费", "生活缴费",
    "休闲娱乐", "医疗教育", "转账往来", "其他支出",
]

# seed 类目 → 标准类目
SEED_TO_STD = {
    "收入": "工资收入",
    "餐饮": "餐饮美食",
    "交通": "交通出行",
    "购物": "购物消费",
    "居住": "生活缴费",
    "订阅": "生活缴费",
    "娱乐": "休闲娱乐",
    "医疗": "医疗教育",
    "其他": "其他支出",
    "未知": "其他支出",
}

# 其他支出占比超过该阈值时输出「分类覆盖不足」提示（规划文档 §3.1）
OTHER_RATIO_WARN = 0.10


def normalize_category(category: str, txn_type: str = "") -> str:
    """seed 类目 → 标准 9 类。转账类型交易归入转账往来。"""
    if category == "其他" and txn_type == "transfer":
        return "转账往来"
    if category in SEED_TO_STD:
        return SEED_TO_STD[category]
    return category if category in STD_CATEGORIES else "其他支出"
