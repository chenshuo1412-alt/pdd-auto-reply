# -*- coding: utf-8 -*-
"""
websocket.tests.test_message_consumer —— 消息处理全链路消费器单元测试
====================================================================
本文件用途：验证 websocket.engine.message_consumer.MessageConsumer 的端到端
编排（任务 19.2），覆盖：

- 原始报文解析为 Context 并过滤非客户 / 不可处理消息；
- 决策链命中关键词 → 发送回复并记消息日志（需求 5.6 / 6.3 / 19.1）；
- 黑名单命中 → 不发送、记「黑名单拦截」（需求 12.4）；
- 风控触发 → 记风控日志并暂停回复，推送系统事件通知（需求 13.2 / 18.3）；
- 转人工关键词命中 → 转人工并暂停自动回复（需求 16.3）；
- 商品卡片签名缺失 → 降级为文本回复商品信息（需求 26.3）；
- AI 分支调用 AI 引擎生成回复并记日志（需求 8.1）。

测试以「依赖注入桩」替换数据库与拼多多外部接口副作用，验证编排逻辑本身。
测试框架：pytest。
"""
import asyncio
import json

import pytest

import common.services.reply_dispatch as reply_dispatch

from agent.agent_config import AgentConfig
from agent.ai_reply_engine import AIReplyResult
from channel_pdd.transfer_service import TransferResult
from engine.message_consumer import (
    RESULT_TRANSFERRED,
    MessageConsumer,
    ShopRuntime,
)
from engine.reply_engine import (
    ACTION_AI,
    ACTION_BLACKLISTED,
    ACTION_GOODS_SPECIFIC,
    ACTION_KEYWORD,
    ACTION_RISK_BLOCKED,
    ReplyRules,
    ShopConfig,
)
from engine.keyword_matcher import MATCH_CONTAINS, REPLY_TEXT
from common.utils.time_utils import now_beijing


@pytest.fixture(autouse=True)
def _memory_dispatch(monkeypatch):
    """消费器单测不连数据库；去重持久化由回归测试覆盖。"""
    state = {}

    def admit(shop_id, msg_id, customer_uid=None):
        text_id = str(msg_id or "").strip()
        if not text_id:
            return "missing_msg_id"
        key = (str(shop_id), text_id)
        if key in state:
            return state[key]
        state[key] = "claimed"
        return "owner"

    def finish(shop_id, msg_id, status, detail=""):
        state[(str(shop_id), str(msg_id))] = status

    monkeypatch.setattr(reply_dispatch, "admit", admit)
    monkeypatch.setattr(reply_dispatch, "finish", finish)


# ----------------------------------------------------------------------
# 桩：拼多多消息发送器（记录调用，恒成功）
# ----------------------------------------------------------------------
class StubSender:
    """记录 send_text / send_image 调用的发送器桩。"""

    def __init__(self):
        self.text_calls = []
        self.image_calls = []

    def send_text(self, recipient_uid, content):
        self.text_calls.append((recipient_uid, content))
        return {"success": True}

    def send_image(self, recipient_uid, content):
        self.image_calls.append((recipient_uid, content))
        return {"success": True}


class StubTransferService:
    """转人工 / 商品卡片服务桩。"""

    def __init__(self, card_downgrade=False):
        self.transfer_calls = []
        self.card_calls = []
        self._card_downgrade = card_downgrade

    def transfer_to_human(self, recipient_uid, cs_uid=None, message_content=None):
        self.transfer_calls.append((recipient_uid, message_content))
        return TransferResult(success=True, message="已转人工")

    def send_goods_card(self, recipient_uid, goods_id, biz_type=2):
        self.card_calls.append((recipient_uid, goods_id))
        if self._card_downgrade:
            return TransferResult(success=False, message="签名缺失", downgrade=True)
        return TransferResult(success=True, message="商品卡片已发送")


# ----------------------------------------------------------------------
# 构造辅助
# ----------------------------------------------------------------------
def _raw_text(text="你好", from_uid="cust_1", msg_id=None):
    """构造一条拼多多文本 push 报文。不同文本默认不同 msg_id。"""
    return json.dumps(
        {
            "response": "push",
            "message": {
                "type": 0,
                "sub_type": 2,
                "content": text,
                "from": {"role": "user", "uid": from_uid},
                "to": {"role": "mall_cs", "uid": "cs_1"},
                "msg_id": msg_id or f"{from_uid}:{text}",
            },
        }
    )


def _runtime(
    *,
    keyword_rules=None,
    blacklist=None,
    goods_replies=None,
    ai_enabled=False,
    default_reply=None,
    default_reply_once=False,
    risk_enabled=False,
    session_limit=None,
    window=None,
    transfer_keywords=None,
):
    """构造 ShopRuntime（直接注入决策所需配置与规则）。"""
    cfg = ShopConfig(
        shop_pk=1,
        ai_enabled=ai_enabled,
        default_reply_content=default_reply,
        default_reply_once=default_reply_once,
        risk_enabled=risk_enabled,
        session_reply_limit=session_limit,
        window_seconds=window,
    )
    rules = ReplyRules(
        keyword_rules=keyword_rules or [],
        filter_rules=[],
        blacklist=blacklist or [],
        goods_replies=goods_replies or [],
    )
    return ShopRuntime(
        shop_config=cfg,
        rules=rules,
        agent_config=AgentConfig(ai_enabled=ai_enabled, api_key="k") if ai_enabled else None,
        transfer_keywords=transfer_keywords or [],
    )


def _kw(keyword="你好", reply="关键词回复"):
    return {
        "id": 1,
        "keyword": keyword,
        "match_type": MATCH_CONTAINS,
        "reply_type": REPLY_TEXT,
        "reply_content": reply,
        "priority": 1,
        "enabled": True,
    }


def _make_consumer(runtime, sender=None, transfer=None):
    """构造注入桩的消费器，并捕获消息 / 风控日志与通知。"""
    msg_logs = []
    risk_logs = []
    notifies = []
    consumer = MessageConsumer(
        shop_id="shop_1",
        shop_pk=1,
        user_id=9,
        runtime_loader=lambda shop_pk: runtime,
        sender=sender or StubSender(),
        transfer_service=transfer or StubTransferService(),
        log_writer=lambda v: msg_logs.append(v),
        risk_log_writer=lambda v: risk_logs.append(v),
        notifier=lambda e, c: notifies.append((e, c)),
    )
    return consumer, msg_logs, risk_logs, notifies


def _run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------------
# 用例
# ----------------------------------------------------------------------
def test_keyword_reply_full_chain():
    """关键词命中：发送文本回复并记一条消息日志（auto_reply）。"""
    runtime = _runtime(keyword_rules=[_kw()])
    sender = StubSender()
    consumer, msg_logs, _, _ = _make_consumer(runtime, sender=sender)

    outcome = _run(consumer.consume_raw(_raw_text("你好呀")))

    assert outcome.handled is True
    assert outcome.action == ACTION_KEYWORD
    assert outcome.replied is True
    assert sender.text_calls == [("cust_1", "关键词回复")]
    assert len(msg_logs) == 1
    assert msg_logs[0]["process_result"] == "auto_reply"
    assert msg_logs[0]["reply_content"] == "关键词回复"


def test_blacklist_no_reply():
    """黑名单命中：不发送回复，记「黑名单拦截」。"""
    runtime = _runtime(
        keyword_rules=[_kw()],
        blacklist=[{"customer_uid": "cust_1", "is_active": True}],
    )
    sender = StubSender()
    consumer, msg_logs, _, _ = _make_consumer(runtime, sender=sender)

    outcome = _run(consumer.consume_raw(_raw_text("你好", from_uid="cust_1")))

    assert outcome.action == ACTION_BLACKLISTED
    assert outcome.replied is False
    assert sender.text_calls == []
    assert msg_logs[0]["process_result"] == "blacklisted"


def test_risk_blocked_writes_risk_log_and_notifies():
    """风控触发：记风控日志、暂停回复并推送系统事件通知。"""
    runtime = _runtime(
        keyword_rules=[_kw()],
        risk_enabled=True,
        session_limit=1,
        window=60,
    )
    consumer, msg_logs, risk_logs, notifies = _make_consumer(runtime)
    # 预置一次会话回复记录，使窗口内计数达上限
    consumer._session_reply_times["cust_1"].append(now_beijing())

    outcome = _run(consumer.consume_raw(_raw_text("你好", from_uid="cust_1")))

    assert outcome.action == ACTION_RISK_BLOCKED
    assert outcome.replied is False
    assert len(risk_logs) == 1
    assert risk_logs[0]["risk_type"] == "frequency_limit"
    assert any(e == "risk_triggered" for e, _ in notifies)
    assert msg_logs[0]["process_result"] == "risk_paused"


def test_transfer_keyword_triggers_transfer():
    """命中转人工关键词：转人工并暂停自动回复，不发送普通回复。"""
    runtime = _runtime(keyword_rules=[_kw()], transfer_keywords=["人工"])
    sender = StubSender()
    transfer = StubTransferService()
    consumer, _, _, _ = _make_consumer(runtime, sender=sender, transfer=transfer)

    outcome = _run(consumer.consume_raw(_raw_text("我要转人工", from_uid="cust_1")))

    assert outcome.transferred is True
    assert outcome.action == RESULT_TRANSFERRED
    assert transfer.transfer_calls == [("cust_1", "我要转人工")]
    assert sender.text_calls == []


def test_ai_branch_generates_reply(monkeypatch):
    """AI 分支：调用 AI 引擎生成回复并记日志（ai_reply）。"""
    runtime = _runtime(ai_enabled=True)
    sender = StubSender()
    consumer, msg_logs, _, _ = _make_consumer(runtime, sender=sender)

    async def _fake_generate(query, config, **kwargs):
        return AIReplyResult(success=True, content="AI回复内容", log_result="ai_reply")

    monkeypatch.setattr(
        "engine.message_consumer.ai_reply_engine.generate_reply", _fake_generate
    )

    outcome = _run(consumer.consume_raw(_raw_text("随便问问", from_uid="cust_1")))

    assert outcome.action == ACTION_AI
    assert outcome.replied is True
    assert sender.text_calls == [("cust_1", "AI回复内容")]
    assert msg_logs[0]["process_result"] == "ai_reply"


def test_non_customer_message_ignored():
    """客服 / 系统消息不进入处理链（handled=False）。"""
    runtime = _runtime(keyword_rules=[_kw()])
    consumer, msg_logs, _, _ = _make_consumer(runtime)

    raw = json.dumps(
        {
            "response": "push",
            "message": {
                "type": 0,
                "sub_type": 2,
                "content": "客服消息",
                "from": {"role": "mall_cs", "uid": "cs_1"},
                "to": {"role": "user", "uid": "cust_1"},
            },
        }
    )
    outcome = _run(consumer.consume_raw(raw))

    assert outcome.handled is False
    assert msg_logs == []


def test_default_reply_once_records_and_skips_second_time():
    """默认回复「只回复一次」：首条发送并登记记录，第二条因已登记而不再发送。"""
    runtime = _runtime(default_reply="默认兜底回复", default_reply_once=True)
    sender = StubSender()
    # 以内存集合模拟「已回复客户」记录表（shop_pk, customer_uid）
    sent_records = set()
    consumer = MessageConsumer(
        shop_id="shop_1",
        shop_pk=1,
        user_id=9,
        runtime_loader=lambda shop_pk: runtime,
        sender=sender,
        transfer_service=StubTransferService(),
        log_writer=lambda v: None,
        risk_log_writer=lambda v: None,
        default_reply_record_reader=lambda shop_pk, uid: (shop_pk, uid) in sent_records,
        default_reply_record_writer=lambda shop_pk, uid: sent_records.add((shop_pk, uid)),
    )

    # 第一条：未命中关键词/AI，走默认回复并发送、登记记录
    first = _run(consumer.consume_raw(_raw_text("随便问问", from_uid="cust_1")))
    assert first.action == "default"
    assert first.replied is True
    assert sender.text_calls == [("cust_1", "默认兜底回复")]
    assert (1, "cust_1") in sent_records

    # 第二条：同一客户已登记 → 跳过默认回复（no_match），不再发送
    second = _run(consumer.consume_raw(_raw_text("再问一次", from_uid="cust_1")))
    assert second.replied is False
    assert second.action == "no_match"
    # 发送记录仍只有第一条
    assert sender.text_calls == [("cust_1", "默认兜底回复")]


def test_default_reply_once_independent_per_customer():
    """默认回复「只回复一次」按客户隔离：不同客户各自都能收到一次默认回复。"""
    runtime = _runtime(default_reply="默认兜底回复", default_reply_once=True)
    sender = StubSender()
    sent_records = set()
    consumer = MessageConsumer(
        shop_id="shop_1",
        shop_pk=1,
        user_id=9,
        runtime_loader=lambda shop_pk: runtime,
        sender=sender,
        transfer_service=StubTransferService(),
        log_writer=lambda v: None,
        risk_log_writer=lambda v: None,
        default_reply_record_reader=lambda shop_pk, uid: (shop_pk, uid) in sent_records,
        default_reply_record_writer=lambda shop_pk, uid: sent_records.add((shop_pk, uid)),
    )

    a = _run(consumer.consume_raw(_raw_text("你好", from_uid="cust_A")))
    b = _run(consumer.consume_raw(_raw_text("你好", from_uid="cust_B")))

    assert a.replied is True
    assert b.replied is True
    assert ("cust_A", "默认兜底回复") in [(u, c) for u, c in sender.text_calls]
    assert ("cust_B", "默认兜底回复") in [(u, c) for u, c in sender.text_calls]
