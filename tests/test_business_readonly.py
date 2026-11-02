"""业务写策略边界测试。

本文件在导入 app 模块前把 BUSINESS_WRITES 设为 readonly，验证只读档的边界；
另用 importlib.reload 验证默认档 all（全部写能力开放）与 cancel_only 档的动作粒度。
"""
from __future__ import annotations

import importlib
import os
import unittest
from unittest.mock import patch

os.environ["BUSINESS_WRITES"] = "readonly"  # 必须先于任何 app.* 导入

from app.core import business_policy as bp  # noqa: E402

# 若本进程先前已按其他档位加载过策略（如与其他测试文件同进程运行），
# 把策略模块与规划层的档位绑定强制刷成 readonly，保证本文件边界测试确定性。
# 说明：reload 复用同一模块命名空间，工具/确认服务持有的函数经 __globals__
# 始终读到最新档位；只有 stub_planner 的 from-import 常量 DENIED_INTENTS 需要刷新。
if bp.WRITES_MODE != "readonly":
    importlib.reload(bp)
    importlib.reload(importlib.import_module("app.planning.stub_planner"))

from app.planning import stub_planner as SP  # noqa: E402
from app.services import confirmation_service  # noqa: E402
from app.tools import subscription_write, transfer_tools  # noqa: E402
from app.tools.registry import TOOL_IMPLS  # noqa: E402


class TestReadonlyMode(unittest.TestCase):
    """readonly 档：全部业务写拒绝。"""

    def test_mode_is_readonly(self):
        self.assertEqual(bp.WRITES_MODE, "readonly")
        self.assertEqual(
            bp.DENIED_INTENTS,
            {"F3.TRANSFER", "F3.SCHED_CREATE", "F3.SCHED_CANCEL", "F2.SUB_CANCEL"})

    def test_write_intents_never_dispatch_or_create_card(self):
        for intent in sorted(SP._WRITE_INTENTS - {'F3.AA_SPLIT'}):
            with self.subTest(intent=intent):
                s = {'last_pending_id': 'stale', 'pending_slots': {'intent': intent}}
                with patch.object(confirmation_service, 'create') as create:
                    blocks, tools, pending = SP._dispatch(s, '转给王芳 3500', intent)
                self.assertIsNone(pending)
                self.assertEqual(tools, [])
                self.assertIn('只读', blocks[0]['text'])
                self.assertNotIn('last_pending_id', s)
                create.assert_not_called()

    def test_write_rules_are_policy_marked_before_llm(self):
        cases = [
            '转给王芳 3500', '创建每月定时转账', '取消定时任务', '取消星橙视频订阅'
        ]
        for q in cases:
            with self.subTest(q=q), patch('app.planning.llm.classify_intent') as llm:
                intent, source, reason = SP.classify_intent(q, None)
                self.assertIn(intent, SP._WRITE_INTENTS)
                self.assertEqual(source, 'policy_readonly')
                self.assertIsNone(reason)
                llm.assert_not_called()

    def test_direct_registered_write_tools_are_denied(self):
        calls = [
            lambda: subscription_write.subscriptions_cancel(['sub-cloud']),
            lambda: transfer_tools.transfers_create('contact-1', 100, 'req-123'),
            lambda: transfer_tools.transfers_scheduled_create('contact-1', 100, 5),
            lambda: transfer_tools.transfers_scheduled_cancel('task-123'),
        ]
        for call in calls:
            with self.assertRaises(bp.BusinessReadOnlyError):
                call()
        for action in ('subscriptions.cancel', 'transfers.create',
                       'transfers.scheduled.create', 'transfers.scheduled.cancel'):
            with self.subTest(action=action), self.assertRaises(bp.BusinessReadOnlyError):
                TOOL_IMPLS[action]()

    def test_services_and_scheduler_have_direct_guards(self):
        with self.assertRaises(bp.BusinessReadOnlyError):
            confirmation_service.create('user', 'transfers.create', 'L2', {}, '', [])
        with self.assertRaises(bp.BusinessReadOnlyError):
            confirmation_service.create('user', 'subscriptions.cancel', 'L1', {}, '', [])
        with self.assertRaises(bp.BusinessReadOnlyError):
            transfer_tools.run_due_tasks('2026-11-01')
        # expire_pending 只把 pending 置为 expired（不产生业务写入），不设写门禁
        self.assertFalse(confirmation_service.expire_pending('no-such-card'))

    def test_lower_level_execution_cannot_bypass_policy(self):
        with self.assertRaises(bp.BusinessReadOnlyError):
            transfer_tools._execute_transfer(None, 'x', 100, 'req')


class TestModeSwitch(unittest.TestCase):
    """模式开关本身：默认 all（恢复原功能），cancel_only 只放行退订。"""

    def _reload_with(self, value: str | None):
        old = os.environ.get("BUSINESS_WRITES")
        if value is None:
            os.environ.pop("BUSINESS_WRITES", None)
        else:
            os.environ["BUSINESS_WRITES"] = value
        try:
            return importlib.reload(bp)
        finally:
            if old is None:
                os.environ.pop("BUSINESS_WRITES", None)
            else:
                os.environ["BUSINESS_WRITES"] = old

    def tearDown(self):
        importlib.reload(bp)  # 恢复文件顶部 readonly 档的模块状态

    def test_default_mode_is_all_and_restores_original_capabilities(self):
        reloaded = self._reload_with(None)
        self.assertEqual(reloaded.WRITES_MODE, "all")
        self.assertEqual(reloaded.DENIED_INTENTS, frozenset())
        for action in ("subscriptions.cancel", "transfers.create",
                       "transfers.scheduled.create", "transfers.scheduled.cancel"):
            self.assertTrue(reloaded.action_write_enabled(action))
            reloaded.require_write_enabled(action)  # 不抛即通过
        reloaded.require_write_enabled()  # 底层资金执行入口放行
        self.assertNotIn("F2.SUB_CANCEL", reloaded.DENIED_INTENTS)

    def test_invalid_value_falls_back_to_all(self):
        reloaded = self._reload_with("nonsense")
        self.assertEqual(reloaded.WRITES_MODE, "all")

    def test_cancel_only_allows_subscription_cancel_only(self):
        reloaded = self._reload_with("cancel_only")
        self.assertEqual(reloaded.WRITES_MODE, "cancel_only")
        self.assertEqual(reloaded.DENIED_INTENTS,
                         {"F3.TRANSFER", "F3.SCHED_CREATE", "F3.SCHED_CANCEL"})
        self.assertTrue(reloaded.action_write_enabled("subscriptions.cancel"))
        for action in ("transfers.create", "transfers.scheduled.create",
                       "transfers.scheduled.cancel"):
            self.assertFalse(reloaded.action_write_enabled(action))
            with self.assertRaises(bp.BusinessReadOnlyError):
                reloaded.require_write_enabled(action)

    def test_aa_split_is_readonly_calculation(self):
        self.assertNotIn('F3.AA_SPLIT', SP._WRITE_INTENTS - {'F3.AA_SPLIT'})
        self.assertNotIn('F3.AA_SPLIT', bp.DENIED_INTENTS)


if __name__ == '__main__':
    unittest.main()
