# -*- coding: utf-8 -*-
"""默认安全模式与去重回归。禁止真实平台连接。"""
from __future__ import annotations

import asyncio
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent.agent_config import AgentConfig
from agent.llm_client import LLMClient
from channel_pdd.api.send_message import SendMessage, SendResult
from channel_pdd.pdd_channel import PDDChannel
from common.core.config import reload_settings
from common.core.platform_guard import PlatformDenied
from sqlalchemy import text

from common.db.session import get_engine, reset_engine
from common.models.reply_dispatch import ReplyDispatch
from common.services import ai_provider_service, reply_dispatch
from common.services.ai_provider_service import AiProviderError
from engine.message_consumer import MessageConsumer, ShopRuntime
from engine.reply_engine import ReplyRules, ShopConfig


class _Block:
    def __init__(self):
        self.hits = []
        self._real = socket.socket.connect

    def install(self, monkeypatch):
        real = self._real
        hits = self.hits

        def connect(sock, address):
            host = address[0] if isinstance(address, tuple) else ""
            port = address[1] if isinstance(address, tuple) and len(address) > 1 else None
            if port in (80, 443) or (isinstance(host, str) and host not in ("127.0.0.1", "::1", "localhost")):
                hits.append(address)
                raise OSError("blocked platform egress")
            return real(sock, address)

        monkeypatch.setattr(socket.socket, "connect", connect)


def _text(msg_id: str, content: str = "什么时候发货") -> dict:
    return {
        "response": "push",
        "message": {
            "type": 0,
            "content": content,
            "msg_id": msg_id,
            "from": {"role": "user", "uid": "buyer-1"},
            "to": {"role": "mall_cs", "uid": "cs-1"},
        },
    }


class _Sender:
    def __init__(self, status="sent"):
        self.status = status
        self.calls = []

    def send_text(self, uid, content):
        self.calls.append((uid, content))
        return SendResult(self.status, {"success": True} if self.status == "sent" else None, self.status)

    def send_image(self, uid, content):
        return self.send_text(uid, content)


def _consumer(shop_id: str, sender: _Sender) -> MessageConsumer:
    runtime = ShopRuntime(
        shop_config=ShopConfig(shop_pk=1, ai_enabled=False, business_enabled=True),
        rules=ReplyRules(keyword_rules=[{
            "keyword": "发货",
            "match_type": "contains",
            "reply_type": "text",
            "reply_content": "48小时内",
            "priority": 1,
            "enabled": True,
        }]),
        agent_config=AgentConfig(ai_enabled=False),
    )
    return MessageConsumer(
        shop_id=shop_id,
        shop_pk=1,
        user_id=1,
        runtime_loader=lambda _pk: runtime,
        sender=sender,
        log_writer=lambda _v: None,
        risk_log_writer=lambda _v: None,
        notifier=None,
        default_reply_record_reader=lambda *_a, **_k: False,
        default_reply_record_writer=lambda *_a, **_k: None,
        chat_message_writer=lambda _v: None,
        chat_history_reader=lambda *_a, **_k: [],
        product_context_reader=lambda *_a, **_k: None,
    )


def test_safe_mode_makes_no_platform_socket(monkeypatch):
    """默认模式不取 token、不建连、不发送，且没有 80/443 或非回环连接。"""
    monkeypatch.setenv("PDD_NETWORK_MODE", "safe")
    monkeypatch.delenv("PDD_HTTP_BASE_URL", raising=False)
    monkeypatch.delenv("PDD_WS_BASE_URL", raising=False)
    monkeypatch.setenv("PDD_SEND_ENABLED", "0")
    block = _Block()
    block.install(monkeypatch)
    calls = {"token": 0}

    def token(*_a, **_k):
        calls["token"] += 1
        return "should-not-run"

    channel = PDDChannel(shop_id="shop-real", user_id=1, token_provider=token)
    assert asyncio.run(channel.start()) is False
    assert calls["token"] == 0

    sender = SendMessage()
    sender.shop_id = "shop-real"
    sender.cookies = {"PASS_ID": "not-real"}
    delivered = sender.send_text("buyer", "你好")
    assert delivered.status == "failed"
    assert block.hits == []

    async def _chat():
        client = LLMClient("sk", "", "cloud-model", 0.0, 1.0)
        await client.chat([{"role": "user", "content": "hi"}])

    with pytest.raises(RuntimeError, match="未配置 API 地址"):
        asyncio.run(_chat())
    with pytest.raises(AiProviderError, match="未配置 API 地址"):
        ai_provider_service.normalize_openai_base_url("")
    assert block.hits == []


def test_explicit_loopback_base_is_used(monkeypatch):
    """显式回环地址会进入客户端；空地址不会。"""
    monkeypatch.setenv("PDD_NETWORK_MODE", "safe")
    client = LLMClient("sk", "http://127.0.0.1:9/v1", "local", 0.0, 1.0)
    assert str(client._ensure_client().base_url).rstrip("/") == "http://127.0.0.1:9/v1"


@pytest.fixture()
def dispatch_db(monkeypatch):
    monkeypatch.setenv("MYSQL_HOST", "127.0.0.1")
    monkeypatch.setenv("MYSQL_PORT", "3306")
    monkeypatch.setenv("MYSQL_USER", "pdd")
    monkeypatch.setenv("MYSQL_PASSWORD", "pdd_test_only")
    monkeypatch.setenv("MYSQL_DATABASE", "pdd_auto_reply")
    monkeypatch.setenv("DATA_ENCRYPT_KEY", "test-only-encrypt")
    monkeypatch.setenv("PDD_NETWORK_MODE", "loopback")
    monkeypatch.setenv("PDD_SEND_ENABLED", "1")
    monkeypatch.setenv("PDD_ALLOW_SHOP_IDS", "shop-a,shop-b")
    monkeypatch.setenv("PDD_HTTP_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("BACKEND_WEB_SERVICE_URL", "http://127.0.0.1:9")
    try:
        reload_settings()
        reset_engine()
        ReplyDispatch.__table__.create(get_engine(), checkfirst=True)
        with get_engine().begin() as conn:
            conn.execute(text("DELETE FROM pdd_reply_dispatch"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"需要本机 MySQL: {exc}")
    yield
    reset_engine()


def test_duplicate_concurrent_restart_and_shop_isolation(dispatch_db):
    sender_a = _Sender()
    consumer_a = _consumer("shop-a", sender_a)
    asyncio.run(consumer_a.consume_raw(_text("m-1")))
    asyncio.run(consumer_a.consume_raw(_text("m-1")))
    assert len(sender_a.calls) == 1

    other = _consumer("shop-a", _Sender())
    asyncio.run(other.consume_raw(_text("m-1")))
    assert other._sender.calls == []

    fresh = _Sender()
    asyncio.run(_consumer("shop-a", fresh).consume_raw(_text("m-2", "发货吗")))
    assert len(fresh.calls) == 1

    shop_b = _Sender()
    asyncio.run(_consumer("shop-b", shop_b).consume_raw(_text("m-1")))
    assert len(shop_b.calls) == 1

    unknown = _Sender("unknown")
    asyncio.run(_consumer("shop-a", unknown).consume_raw(_text("m-unknown")))
    assert len(unknown.calls) == 1
    again = _Sender("sent")
    asyncio.run(_consumer("shop-a", again).consume_raw(_text("m-unknown")))
    assert again.calls == []

    barrier = threading.Barrier(2)
    results = []

    def race():
        barrier.wait()
        results.append(reply_dispatch.admit("shop-a", "m-race", "buyer-1"))

    threads = [threading.Thread(target=race) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count("owner") == 1


def test_unknown_http_send_is_not_retried(monkeypatch):
    """真实 SendMessage 遇到不响应的回环服务只打一次，再来同一 msg_id 不再打。"""
    hits = {"n": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            hits["n"] += 1
            import time
            time.sleep(3)

        def log_message(self, *_a):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("PDD_NETWORK_MODE", "loopback")
    monkeypatch.setenv("PDD_SEND_ENABLED", "1")
    monkeypatch.setenv("PDD_ALLOW_SHOP_IDS", "shop-a")
    monkeypatch.setenv("PDD_HTTP_BASE_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("PDD_SEND_TIMEOUT", "0.4")
    monkeypatch.setenv("MYSQL_HOST", "127.0.0.1")
    monkeypatch.setenv("MYSQL_PORT", "3306")
    monkeypatch.setenv("MYSQL_USER", "pdd")
    monkeypatch.setenv("MYSQL_PASSWORD", "pdd_test_only")
    monkeypatch.setenv("MYSQL_DATABASE", "pdd_auto_reply")
    monkeypatch.setenv("BACKEND_WEB_SERVICE_URL", "http://127.0.0.1:9")
    try:
        reload_settings()
        reset_engine()
        ReplyDispatch.__table__.create(get_engine(), checkfirst=True)
        with get_engine().begin() as conn:
            conn.execute(text("DELETE FROM pdd_reply_dispatch"))
    except Exception as exc:  # noqa: BLE001
        server.shutdown()
        pytest.skip(f"需要本机 MySQL: {exc}")

    sender = _Sender("unknown")
    # 先用假发送器确认账本；再用真实 SendMessage 打一次超时。
    real = SendMessage()
    real.shop_id = "shop-a"
    real.cookies = {}
    first = real.send_text("buyer-1", "48小时内")
    assert first.status == "unknown"
    assert hits["n"] == 1
    unknown = _Sender("unknown")
    asyncio.run(_consumer("shop-a", unknown).consume_raw(_text("m-http-2")))
    assert len(unknown.calls) == 1
    follow = _Sender("sent")
    asyncio.run(_consumer("shop-a", follow).consume_raw(_text("m-http-2")))
    assert follow.calls == []
    assert hits["n"] == 1
    server.shutdown()


def test_reconnect_replay_does_not_resend(dispatch_db, monkeypatch):
    import json
    import websockets

    sent = asyncio.Event()
    opened = {"n": 0}

    async def handler(ws):
        opened["n"] += 1
        await ws.send(json.dumps(_text("m-replay")))
        if opened["n"] == 1:
            await sent.wait()
            await ws.close()
        else:
            await asyncio.sleep(0.3)

    async def run():
        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setenv("PDD_WS_BASE_URL", f"ws://127.0.0.1:{port}/")
        monkeypatch.setenv("PDD_RECONNECT_HEALTHY_SECONDS", "0")
        monkeypatch.setenv("PDD_PRESENCE_ENABLED", "0")
        sender = _Sender()
        consumer = _consumer("shop-a", sender)
        from channel_pdd.connection_manager import create_channel
        from channel_pdd.core.pdd_config import HeartbeatConfig, ReconnectConfig

        channel = create_channel("shop-a", 1, 1, enable_notify=False, consumer=consumer)
        channel.token_provider = lambda *_a, **_k: "mock-token"
        channel._token_provider = channel.token_provider
        channel.heartbeat_config = HeartbeatConfig(enable_heartbeat=False)
        channel.reconnect_config = ReconnectConfig(
            max_attempts=3, initial_delay=0.05, max_delay=0.1, backoff_factor=2
        )
        assert await channel.start() is True
        for _ in range(50):
            if sender.calls:
                break
            await asyncio.sleep(0.05)
        sent.set()
        await asyncio.sleep(0.4)
        await channel.stop()
        server.close()
        await server.wait_closed()
        assert opened["n"] >= 2
        assert len(sender.calls) == 1

    asyncio.run(run())
