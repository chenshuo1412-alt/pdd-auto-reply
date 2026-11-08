# -*- coding: utf-8 -*-
"""受控实店授权、测试买家白名单，以及空模型地址不出站。"""
from __future__ import annotations

import asyncio
import json
import socket
from datetime import timedelta
from pathlib import Path

import pytest

from agent.llm_client import LLMClient
from channel_pdd.api.send_message import SendMessage
from channel_pdd.api.set_cs_status import set_cs_online
from channel_pdd.core.base_request import BaseRequest
from channel_pdd.pdd_channel import PDDChannel
from common.core.live_grant import buyer_granted, read_grant, stable_buyer_uid
from common.core.platform_guard import decide, network_mode
from common.services import ai_provider_service
from common.services.ai_provider_service import AiProviderError
from common.utils.time_utils import now_beijing
from engine.message_consumer import MessageConsumer, ShopRuntime
from engine.reply_engine import ReplyRules, ShopConfig
from channel_pdd.api.send_message import SendResult


def _grant(path: Path, shop: str, buyers: list[str], *, confirmed=True, hours=1) -> None:
    payload = {
        "confirmed": confirmed,
        "shop_id": shop,
        "buyer_uids": buyers,
        "expires_at": (now_beijing() + timedelta(hours=hours)).isoformat(),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _use(monkeypatch, path: Path, mode="live"):
    monkeypatch.setenv("PDD_NETWORK_MODE", mode)
    monkeypatch.setenv("PDD_LIVE_GRANT_PATH", str(path))
    monkeypatch.setenv("PDD_SEND_ENABLED", "0")
    monkeypatch.setenv("PDD_PRESENCE_ENABLED", "0")
    monkeypatch.delenv("PDD_HTTP_BASE_URL", raising=False)
    monkeypatch.delenv("PDD_WS_BASE_URL", raising=False)


class _Net:
    def __init__(self):
        self.hits = []

    def install(self, monkeypatch):
        real = socket.socket.connect
        hits = self.hits

        def connect(sock, address):
            host = address[0] if isinstance(address, tuple) else ""
            port = address[1] if isinstance(address, tuple) and len(address) > 1 else None
            if port in (80, 443) or host not in ("127.0.0.1", "::1", "localhost"):
                hits.append(address)
                raise OSError("blocked")
            return real(sock, address)

        monkeypatch.setattr(socket.socket, "connect", connect)


def test_unconfirmed_live_and_unknown_mode_stay_closed(tmp_path, monkeypatch):
    path = tmp_path / "grant.json"
    _grant(path, "shop-a", ["buyer-a"], confirmed=False)
    _use(monkeypatch, path)
    net = _Net()
    net.install(monkeypatch)
    assert network_mode() == "live"
    assert decide("connect", shop_id="shop-a", url="wss://m-ws.pinduoduo.com/")[0] is False
    channel = PDDChannel(shop_id="shop-a", user_id=1, token_provider=lambda *_a, **_k: "nope")
    assert asyncio.run(channel.start()) is False
    monkeypatch.setenv("PDD_NETWORK_MODE", "mystery")
    assert network_mode() == "safe"
    assert decide("connect", shop_id="shop-a")[0] is False
    assert net.hits == []


def test_grant_requires_shop_and_explicit_buyers(tmp_path, monkeypatch):
    path = tmp_path / "grant.json"
    _use(monkeypatch, path)
    _grant(path, "shop-a", ["buyer-a", "buyer-b"])
    assert buyer_granted("shop-a", "buyer-a")[0] is True
    assert buyer_granted("shop-a", "buyer-b")[0] is True
    assert buyer_granted("shop-a", "buyer-c")[0] is False
    assert buyer_granted("shop-b", "buyer-a")[0] is False
    assert buyer_granted("shop-a", None)[0] is False
    assert buyer_granted("shop-a", "  ")[0] is False
    assert buyer_granted("shop-a", "张三")[0] is False
    assert stable_buyer_uid("*") is None

    _grant(path, "shop-a", [])
    assert read_grant()[0] is None
    _grant(path, "shop-a", ["*"])
    assert read_grant()[0] is None
    _grant(path, "shop-a", ["buyer-a"], hours=-1)
    assert "过期" in read_grant()[1]
    path.unlink()
    assert buyer_granted("shop-a", "buyer-a")[0] is False


def test_send_and_presence_stay_off_and_redirect_cannot_escape(tmp_path, monkeypatch):
    path = tmp_path / "grant.json"
    _grant(path, "shop-a", ["buyer-a"])
    _use(monkeypatch, path)
    net = _Net()
    net.install(monkeypatch)
    seen = []

    def fake_post(url, *args, **kwargs):
        seen.append((url, kwargs.get("allow_redirects")))
        raise AssertionError("live send must not be attempted while send is off")

    monkeypatch.setattr("channel_pdd.core.base_request.requests.post", fake_post)
    sender = SendMessage()
    sender.shop_id = "shop-a"
    sender.cookies = {"PASS_ID": "synthetic"}
    assert sender.send_text("buyer-a", "你好").status == "failed"
    assert set_cs_online("shop-a", 1) is False
    assert seen == []
    assert net.hits == []

    monkeypatch.setenv("PDD_NETWORK_MODE", "loopback")
    monkeypatch.setenv("PDD_HTTP_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("PDD_ALLOW_SHOP_IDS", "shop-a")
    monkeypatch.setenv("PDD_SEND_ENABLED", "1")

    class _Resp:
        status_code = 302
        headers = {"Location": "https://evil.example/steal"}
        text = ""

        def json(self):
            return {}

    def redirect_post(url, *args, **kwargs):
        seen.append((url, kwargs.get("allow_redirects")))
        return _Resp()

    monkeypatch.setattr("channel_pdd.core.base_request.requests.post", redirect_post)
    request = BaseRequest()
    request.shop_id = "shop-a"
    request.post("https://mms.pinduoduo.com/plateau/chat/send_message", json_data={}, purpose="send", customer_uid="buyer-a", allow_redirects=True)
    assert seen == [("http://127.0.0.1:9/plateau/chat/send_message", False)]


def test_live_official_host_only_and_login_stays_closed(tmp_path, monkeypatch):
    path = tmp_path / "grant.json"
    _grant(path, "shop-a", ["buyer-a"])
    _use(monkeypatch, path)
    assert decide("connect", shop_id="shop-a", url="wss://m-ws.pinduoduo.com/")[0] is True
    assert decide("token", shop_id="shop-a", url="https://mms.pinduoduo.com/chats/getToken")[0] is True
    assert decide("connect", shop_id="shop-a", url="https://evil.example/mms")[0] is False
    assert decide("connect", shop_id="other", url="wss://m-ws.pinduoduo.com/")[0] is False
    assert decide("connect", shop_id="shop-a", url="https://user:pass@mms.pinduoduo.com/x")[0] is False
    assert decide("login", shop_id="shop-a", url="https://mms.pinduoduo.com/login")[0] is False
    assert decide("scheduler", shop_id="shop-a")[0] is False
    assert decide("send", shop_id="shop-a", url="https://mms.pinduoduo.com/plateau/chat/send_message", customer_uid="buyer-a")[0] is False


def _consumer(shop_id, sender, loader_calls):
    runtime = ShopRuntime(
        shop_config=ShopConfig(shop_pk=1, ai_enabled=True, business_enabled=True),
        rules=ReplyRules(keyword_rules=[{
            "keyword": "发货",
            "match_type": "contains",
            "reply_type": "text",
            "reply_content": "48小时内",
            "priority": 1,
            "enabled": True,
        }]),
    )

    def loader(_pk):
        loader_calls.append(_pk)
        return runtime

    return MessageConsumer(
        shop_id=shop_id,
        shop_pk=1,
        user_id=1,
        runtime_loader=loader,
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


def _text(uid, msg_id="m1"):
    return {
        "response": "push",
        "message": {
            "type": 0,
            "content": "什么时候发货",
            "msg_id": msg_id,
            "nickname": uid,
            "from": {"role": "user", "uid": uid},
            "to": {"role": "mall_cs", "uid": "cs"},
        },
    }


class _Sender:
    def __init__(self):
        self.calls = []

    def send_text(self, uid, content):
        self.calls.append(uid)
        return SendResult("sent", {"success": True})

    def send_image(self, uid, content):
        return self.send_text(uid, content)


def test_whitelist_blocks_before_model_and_revoke_is_immediate(tmp_path, monkeypatch):
    path = tmp_path / "grant.json"
    _grant(path, "shop-a", ["buyer-a", "buyer-b"])
    _use(monkeypatch, path)
    state = {}

    def admit(shop_id, msg_id, customer_uid=None):
        key = (shop_id, msg_id)
        if key in state:
            return state[key]
        state[key] = "claimed"
        return "owner"

    def finish(shop_id, msg_id, status, detail=""):
        state[(shop_id, msg_id)] = status

    monkeypatch.setattr("common.services.reply_dispatch.admit", admit)
    monkeypatch.setattr("common.services.reply_dispatch.finish", finish)
    loads = []
    sender = _Sender()
    consumer = _consumer("shop-a", sender, loads)
    assert asyncio.run(consumer.consume_raw(_text("buyer-a", "m-a"))).replied is True
    assert asyncio.run(consumer.consume_raw(_text("buyer-b", "m-b"))).replied is True
    denied = asyncio.run(consumer.consume_raw(_text("buyer-c", "m-c")))
    assert denied.replied is False
    assert denied.action == "buyer_denied"
    nameless = {"response": "push", "message": {"type": 0, "content": "什么时候发货", "msg_id": "m-n", "nickname": "buyer-a", "from": {"role": "user"}, "to": {"role": "mall_cs"}}}
    assert asyncio.run(consumer.consume_raw(nameless)).action == "buyer_denied"
    other = _consumer("shop-b", _Sender(), [])
    assert asyncio.run(other.consume_raw(_text("buyer-a", "m-x"))).action == "buyer_denied"
    assert sender.calls == ["buyer-a", "buyer-b"]
    assert loads == [1, 1]

    path.unlink()
    after = _Sender()
    assert asyncio.run(_consumer("shop-a", after, []).consume_raw(_text("buyer-a", "m-new"))).action == "buyer_denied"
    assert after.calls == []


def test_empty_model_addresses_do_not_call_cloud(monkeypatch):
    hits = []

    def urlopen(request, timeout=0):
        hits.append(getattr(request, "full_url", str(request)))
        raise AssertionError("empty address must not egress")

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    for builder in (
        lambda: ai_provider_service._build_anthropic_url("", "/messages"),
        lambda: ai_provider_service._build_gemini_url("   ", "/models"),
        lambda: ai_provider_service.test_ai_connection("anthropic", "", "k", "claude"),
        lambda: ai_provider_service.test_ai_connection("gemini", None, "k", "gemini"),
        lambda: ai_provider_service.test_ai_connection("openai_compatible", "", "k", "local"),
        lambda: ai_provider_service.fetch_model_list("openai_compatible", "", "k"),
    ):
        with pytest.raises(AiProviderError):
            builder()

    async def chat(base, provider):
        client = LLMClient("sk", base, "m", 0, 1, provider_type=provider)
        await client.chat([{"role": "user", "content": "hi"}])

    for provider in ("openai_compatible", "anthropic", "gemini", "dashscope_app"):
        with pytest.raises(RuntimeError):
            asyncio.run(chat("", provider))
    assert hits == []
    vue = (
        Path(__file__).resolve().parents[2]
        / "frontend/src/components/shop_settings/AiSettingsPanel.vue"
    ).read_text(encoding="utf-8")
    assert "form.api_base = opt.default_base_url" not in vue
    assert "空白地址不会改用云端默认" in vue
