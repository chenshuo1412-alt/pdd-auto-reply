# -*- coding: utf-8 -*-
"""
test_pdd_channel —— 拼多多店铺连接服务 PDDChannel 单元测试
==========================================================
本文件用途：在「外部 WebSocket 用 mock / 测试桩」前提下，验证 task 10.4
连接服务的核心行为（需求 5.1 / 5.2 / 5.6 / 5.8）：
- 建连成功置「已连接」，收到消息按 FIFO 写入消息队列（需求 5.1 / 5.3）。
- 心跳成功记录最近心跳时间（北京时间）（需求 5.2 / 5.8）。
- 回复发送经 WebSocket send（需求 5.6）。
- 指数退避延迟公式正确，达上限置「错误」（需求 5.4 / 5.5）。
- 连接状态查询返回状态与最近心跳时间（需求 5.8）。

测试不发起真实网络连接：通过 monkeypatch 将 websockets.connect 替换为返回
假 WebSocket 的异步上下文管理器；所有协程经 asyncio.run 驱动，不依赖
pytest-asyncio。
"""
from __future__ import annotations

import asyncio

import pytest

import channel_pdd.pdd_channel as channel_mod
from channel_pdd.core.connection_status import (
    ConnectionState,
    ConnectionStatusManager,
)
from channel_pdd.core.pdd_config import HeartbeatConfig, ReconnectConfig
from channel_pdd.pdd_channel import PDDChannel


@pytest.fixture(autouse=True)
def _loopback_channel(monkeypatch):
    """连接单测使用回环地址，默认安全模式不会建连。"""
    monkeypatch.setenv("PDD_NETWORK_MODE", "loopback")
    monkeypatch.setenv("PDD_WS_BASE_URL", "ws://127.0.0.1:9/")
    monkeypatch.setenv("PDD_ALLOW_SHOP_IDS", "shop_a,shop_b,shop_c,shop_d,shop_e")
    monkeypatch.setenv("PDD_SEND_ENABLED", "1")
    monkeypatch.setenv("PDD_PRESENCE_ENABLED", "0")


class _FakeWebSocket:
    """最小化假 WebSocket：可投递若干消息后正常关闭，记录发送内容与 ping 次数。"""

    def __init__(self, incoming=None):
        self._incoming = list(incoming or [])
        self.sent = []
        self.ping_count = 0
        self.closed = False

    def __aiter__(self):
        return self._iterator()

    async def _iterator(self):
        for msg in self._incoming:
            yield msg
        # 消息投递完毕后正常结束（模拟连接关闭）。

    async def send(self, message):
        self.sent.append(message)

    async def ping(self):
        # 真实 websockets.ping() 返回 pong 等待 future；此处以 None 简化，
        # 仅用于验证心跳确实发送过（ping_count 自增）。
        self.ping_count += 1
        return None

    async def close(self):
        self.closed = True


class _FakeConnectCM:
    """websockets.connect 的假替身：异步上下文管理器返回假 WebSocket。"""

    def __init__(self, ws):
        self._ws = ws

    async def __aenter__(self):
        return self._ws

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _patch_connect(monkeypatch, ws):
    """将 websockets.connect 替换为返回指定假 WebSocket 的桩。"""
    monkeypatch.setattr(
        channel_mod.websockets, "connect", lambda *a, **k: _FakeConnectCM(ws)
    )


# ----------------------------------------------------------------------
# 需求 5.1 / 5.3：建连置「已连接」，消息按 FIFO 入队
# ----------------------------------------------------------------------
def test_connect_sets_connected_and_enqueues_messages_in_order(monkeypatch):
    """建连成功置「已连接」，收到的消息按 FIFO 顺序写入内置队列。"""
    fake_ws = _FakeWebSocket(incoming=["m1", "m2", "m3"])
    _patch_connect(monkeypatch, fake_ws)

    manager = ConnectionStatusManager()
    channel = PDDChannel(
        shop_id="shop_a",
        user_id=1,
        username="u1",
        status_manager=manager,
        # 关闭心跳避免无限循环；关闭自动重连只跑单次连接。
        heartbeat_config=HeartbeatConfig(enable_heartbeat=False),
        reconnect_config=ReconnectConfig(enable_auto_reconnect=False),
        token_provider=lambda *a, **k: "fake_token",
    )

    async def _run():
        channel._stop_event = asyncio.Event()
        await channel._connect_once()
        # 在事件循环内排空队列（drain 为异步），返回 FIFO 顺序的 payload 列表。
        return [item.payload for item in await channel.message_queue.drain()]

    drained = asyncio.run(_run())

    # 建连过程中状态曾被置为「已连接」。
    status = manager.get_status("shop_a", 1)
    assert status is not None
    assert status.last_connect_time is not None  # 进入过已连接

    # 消息按 FIFO 顺序入队（默认队列为 FifoMessageQueue）。
    assert drained == ["m1", "m2", "m3"]


# ----------------------------------------------------------------------
# 需求 5.2 / 5.8：心跳成功记录最近心跳时间（北京时间）
# ----------------------------------------------------------------------
def test_heartbeat_records_last_heartbeat_time(monkeypatch):
    """心跳成功后，连接状态记录最近心跳时间。"""
    manager = ConnectionStatusManager()
    manager.update_status("shop_b", 2, "u2", ConnectionState.CONNECTED)
    fake_ws = _FakeWebSocket()
    channel = PDDChannel(
        shop_id="shop_b",
        user_id=2,
        username="u2",
        status_manager=manager,
        heartbeat_config=HeartbeatConfig(
            enable_heartbeat=True, heartbeat_interval=0.01, heartbeat_timeout=0.1
        ),
        token_provider=lambda *a, **k: "t",
    )

    async def _run():
        channel._stop_event = asyncio.Event()
        task = asyncio.create_task(channel._heartbeat_loop(fake_ws))
        await asyncio.sleep(0.05)
        channel._stop_event.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())

    status = manager.get_status("shop_b", 2)
    assert status is not None
    assert status.last_heartbeat is not None
    assert fake_ws.ping_count >= 1


# ----------------------------------------------------------------------
# 需求 5.6：回复经 WebSocket 发送
# ----------------------------------------------------------------------
def test_send_reply_via_websocket(monkeypatch):
    """send_reply 将字典序列化为 JSON 并经 WebSocket 发送。"""
    fake_ws = _FakeWebSocket()
    channel = PDDChannel(shop_id="shop_c", user_id=3, token_provider=lambda *a, **k: "t")
    channel.ws = fake_ws

    async def _run():
        ok = await channel.send_reply({"text": "你好"})
        return ok

    ok = asyncio.run(_run())
    assert ok is True
    assert len(fake_ws.sent) == 1
    assert "你好" in fake_ws.sent[0]


def test_send_reply_without_connection_returns_false():
    """连接不可用时 send_reply 返回 False（不抛异常）。"""
    channel = PDDChannel(shop_id="shop_c", user_id=3, token_provider=lambda *a, **k: "t")
    channel.ws = None
    assert asyncio.run(channel.send_reply("hi")) is False


# ----------------------------------------------------------------------
# 需求 5.4 / 5.5：指数退避延迟公式与达上限置「错误」
# ----------------------------------------------------------------------
def test_reconnect_backoff_delay_formula():
    """退避延迟 = min(initial_delay * backoff_factor**attempt, max_delay)。"""
    cfg = ReconnectConfig(initial_delay=2.0, backoff_factor=2.0, max_delay=60.0)
    assert cfg.compute_delay(0) == 2.0
    assert cfg.compute_delay(1) == 4.0
    assert cfg.compute_delay(2) == 8.0
    # 封顶 max_delay。
    assert cfg.compute_delay(10) == 60.0


def test_reconnect_reaches_limit_sets_error(monkeypatch):
    """Token 始终缺失导致每次连接失败，达上限后置「错误」（需求 5.5）。"""
    manager = ConnectionStatusManager()
    channel = PDDChannel(
        shop_id="shop_d",
        user_id=4,
        username="u4",
        status_manager=manager,
        reconnect_config=ReconnectConfig(
            max_attempts=3, initial_delay=0.001, max_delay=0.002, backoff_factor=2.0
        ),
        token_provider=lambda *a, **k: None,  # 始终拿不到 token → 连接失败
    )

    async def _run():
        await channel.start()
        await channel._connect_task

    asyncio.run(_run())

    status = manager.get_status("shop_d", 4)
    assert status is not None
    assert status.state == ConnectionState.ERROR


# ----------------------------------------------------------------------
# 需求 5.8：连接状态查询返回状态与最近心跳时间
# ----------------------------------------------------------------------
def test_get_connection_status_snapshot():
    """连接状态查询返回含状态文案与最近心跳时间字段的字典。"""
    manager = ConnectionStatusManager()
    manager.update_status("shop_e", 5, "u5", ConnectionState.CONNECTED)
    manager.record_heartbeat("shop_e", 5)
    channel = PDDChannel(shop_id="shop_e", user_id=5, status_manager=manager)

    info = channel.get_connection_status()
    assert info is not None
    assert info["state"] == "connected"
    assert info["state_label"] == "已连接"
    assert info["last_heartbeat_time"] is not None
