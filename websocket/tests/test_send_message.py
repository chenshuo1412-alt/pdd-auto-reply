# -*- coding: utf-8 -*-
"""
test_send_message —— 拼多多消息发送接口 SendMessage 单元测试
============================================================
本文件用途：验证 channel_pdd.api.send_message.SendMessage 的核心行为
（需求 16.1 / 16.2 / 16.4 / 16.5）：
- send_text：成功返回响应；success=true 但携带业务错误码 10002 视为失败返回 None。
- send_mall_goods_card：携带有效签名正常返回；缺签名时（check_signature）抛
  AntiContentMissingError 供上层降级（需求 16.5 / 26.3）。
- get_assign_cs_list：成功解析 csList；失败返回 None。
- move_conversation：成功返回响应；失败返回 None。

测试不依赖真实数据库或网络：
- 构造 SendMessage 时不传 shop_id/user_id（跳过 DB Cookie 加载），手动注入 cookies；
- 通过 monkeypatch 替换 requests.post 为可控假响应。
"""
from __future__ import annotations

import pytest

import channel_pdd.core.base_request as br_mod
from channel_pdd.api.send_message import SendMessage
from channel_pdd.core.anti_content import AntiContentMissingError


class _FakeResponse:
    """最小化假 HTTP 响应。"""

    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {}
        self.text = text

    def json(self):
        return self._json_data


@pytest.fixture(autouse=True)
def _loopback_send(monkeypatch):
    """发送单测只走回环策略。"""
    monkeypatch.setenv("PDD_NETWORK_MODE", "loopback")
    monkeypatch.setenv("PDD_HTTP_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("PDD_ALLOW_SHOP_IDS", "shop1")
    monkeypatch.setenv("PDD_SEND_ENABLED", "1")
    monkeypatch.setattr(br_mod.time, "sleep", lambda *_a, **_k: None)


def _make_sender(cookies=None):
    """构造不触发 DB 加载的 SendMessage，并手动注入 cookies。"""
    sender = SendMessage()
    sender.shop_id = "shop1"
    sender.cookies = cookies or {}
    sender.max_retries = 0
    return sender


# ----------------------------------------------------------------------
# send_text
# ----------------------------------------------------------------------
def test_send_text_success(monkeypatch):
    """发送文本成功返回响应字典。"""
    monkeypatch.setattr(
        br_mod.requests, "post",
        lambda *_a, **_k: _FakeResponse(json_data={"success": True, "result": {}}),
    )
    sender = _make_sender()
    result = sender.send_text("uid_1", "你好")
    assert result.status == "sent"
    assert result.payload["success"] is True


def test_send_text_business_error_returns_none(monkeypatch):
    """success=true 但 result.error_code=10002 视为业务失败返回 None。"""
    monkeypatch.setattr(
        br_mod.requests, "post",
        lambda *_a, **_k: _FakeResponse(
            json_data={"success": True, "result": {"error_code": 10002, "error": "频繁"}}
        ),
    )
    sender = _make_sender()
    assert sender.send_text("uid_1", "你好").status == "failed"


# ----------------------------------------------------------------------
# send_mall_goods_card（依赖签名，需求 16.4 / 16.5）
# ----------------------------------------------------------------------
def test_send_goods_card_with_valid_signature(monkeypatch):
    """携带有效 anti-content 签名时，商品卡片正常发送。"""
    monkeypatch.setattr(
        br_mod.requests, "post",
        lambda *_a, **_k: _FakeResponse(json_data={"success": True}),
    )
    sender = _make_sender(cookies={"anti_content": "valid-sig"})
    result = sender.send_mall_goods_card("uid_1", "goods_99")
    assert result.status == "sent"
    assert result.payload["success"] is True


def test_send_goods_card_missing_signature_raises(monkeypatch):
    """缺少 anti-content 签名时（check_signature），请求前抛领域异常供降级。"""
    called = {"n": 0}
    monkeypatch.setattr(
        br_mod.requests, "post",
        lambda *_a, **_k: called.__setitem__("n", called["n"] + 1)
        or _FakeResponse(json_data={"success": True}),
    )
    sender = _make_sender(cookies={"foo": "bar"})  # 无签名
    with pytest.raises(AntiContentMissingError):
        sender.send_mall_goods_card("uid_1", "goods_99")
    assert called["n"] == 0  # 缺签名不真正发起请求


# ----------------------------------------------------------------------
# get_assign_cs_list（需求 16.1）
# ----------------------------------------------------------------------
def test_get_cs_list_success(monkeypatch):
    """成功解析 csList（拼多多返回为以 cs_uid 为键的字典，规整为列表）。"""
    cs_raw = {"cs_shop_x_1": {"name": "客服一"}, "cs_shop_x_2": {"cs_name": "客服二"}}
    monkeypatch.setattr(
        br_mod.requests, "post",
        lambda *_a, **_k: _FakeResponse(
            json_data={"success": True, "result": {"csList": cs_raw}}
        ),
    )
    sender = _make_sender()
    result = sender.get_assign_cs_list()
    assert result == [
        {"cs_uid": "cs_shop_x_1", "cs_name": "客服一"},
        {"cs_uid": "cs_shop_x_2", "cs_name": "客服二"},
    ]


def test_get_cs_list_accepts_list_shape(monkeypatch):
    """兼容 csList 为列表形态：规整为统一的 cs_uid / cs_name 列表。"""
    cs_raw = [{"csid": "cs1", "name": "客服一"}]
    monkeypatch.setattr(
        br_mod.requests, "post",
        lambda *_a, **_k: _FakeResponse(
            json_data={"success": True, "result": {"csList": cs_raw}}
        ),
    )
    sender = _make_sender()
    assert sender.get_assign_cs_list() == [{"cs_uid": "cs1", "cs_name": "客服一"}]


def test_get_cs_list_missing_cslist_returns_none(monkeypatch):
    """响应缺少 csList（None）时返回 None。"""
    monkeypatch.setattr(
        br_mod.requests, "post",
        lambda *_a, **_k: _FakeResponse(json_data={"success": True, "result": {}}),
    )
    sender = _make_sender()
    assert sender.get_assign_cs_list() is None


def test_get_cs_list_failure_returns_none(monkeypatch):
    """接口失败返回 None。"""
    monkeypatch.setattr(
        br_mod.requests, "post",
        lambda *_a, **_k: _FakeResponse(json_data={"success": False, "result": {}}),
    )
    sender = _make_sender()
    assert sender.get_assign_cs_list() is None


# ----------------------------------------------------------------------
# move_conversation（需求 16.2）
# ----------------------------------------------------------------------
def test_move_conversation_success(monkeypatch):
    """会话转移成功返回响应。"""
    monkeypatch.setattr(
        br_mod.requests, "post",
        lambda *_a, **_k: _FakeResponse(json_data={"success": True}),
    )
    sender = _make_sender()
    assert sender.move_conversation("uid_1", "cs1") is not None


def test_move_conversation_failure_returns_none(monkeypatch):
    """会话转移失败返回 None。"""
    monkeypatch.setattr(
        br_mod.requests, "post",
        lambda *_a, **_k: _FakeResponse(json_data={"success": False}),
    )
    sender = _make_sender()
    assert sender.move_conversation("uid_1", "cs1") is None
