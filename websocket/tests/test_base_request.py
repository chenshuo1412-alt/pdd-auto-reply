# -*- coding: utf-8 -*-
"""
test_base_request —— 拼多多基础请求层 BaseRequest 单元测试
==========================================================
本文件用途：验证 channel_pdd.core.base_request.BaseRequest 的核心能力
（需求 26.1/26.2 与会话过期自动重登）：
- 统一重试：5xx 状态码触发重试，最终返回成功响应。
- 会话过期（error_code=43001）自动重登并重试原请求（仅一次）。
- anti-content 签名缺失：依赖签名接口请求前抛 AntiContentMissingError。
- 响应签名失效：依赖签名接口响应判定为签名失效时抛 AntiContentMissingError。

测试不依赖真实数据库或网络：
- 构造 BaseRequest 时不传 shop_id/user_id（跳过 DB 加载），手动设置 cookies；
- 通过 monkeypatch 替换 requests.get/post 为可控的假响应序列。
"""
from __future__ import annotations

import pytest

import channel_pdd.core.base_request as br_mod
import channel_pdd.core.session_relogin as sr_mod
from channel_pdd.core.anti_content import AntiContentMissingError
from channel_pdd.core.base_request import BaseRequest


class _FakeResponse:
    """最小化的假 HTTP 响应，模拟 requests.Response 的关键属性。"""

    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {}
        self.text = text

    def json(self):
        return self._json_data


@pytest.fixture(autouse=True)
def _loopback_http(monkeypatch):
    """既有请求单测走回环策略，避免打到真实平台地址。"""
    monkeypatch.setenv("PDD_NETWORK_MODE", "loopback")
    monkeypatch.setenv("PDD_HTTP_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("PDD_ALLOW_SHOP_IDS", "shop1")
    monkeypatch.setattr(br_mod.time, "sleep", lambda *_a, **_k: None)


def _make_request(cookies=None):
    """构造不触发 DB 加载的 BaseRequest，并手动注入 cookies。"""
    req = BaseRequest(max_retries=2, retry_delay=0.01)
    req.shop_id = "shop1"
    req.cookies = cookies or {}
    return req


# ----------------------------------------------------------------------
# 统一重试
# ----------------------------------------------------------------------
def test_post_retries_on_5xx_then_succeeds(monkeypatch):
    """首次 503、重试后 200，应最终返回成功响应。"""
    responses = [
        _FakeResponse(status_code=503),
        _FakeResponse(status_code=200, json_data={"success": True, "result": 1}),
    ]
    calls = {"n": 0}

    def fake_post(*_a, **_k):
        resp = responses[calls["n"]]
        calls["n"] += 1
        return resp

    monkeypatch.setattr(br_mod.requests, "post", fake_post)

    req = _make_request()
    result = req.post("https://mms.pinduoduo.com/x", json_data={"a": 1})
    assert result == {"success": True, "result": 1}
    assert calls["n"] == 2  # 一次失败 + 一次成功


# ----------------------------------------------------------------------
# 会话过期自动重登
# ----------------------------------------------------------------------
def test_session_expired_triggers_relogin_then_retry(monkeypatch):
    """error_code=43001 触发自动重登，重登成功后重试得到正常响应。"""
    responses = [
        _FakeResponse(
            status_code=200,
            json_data={"error_code": 43001, "error_msg": "会话已过期"},
        ),
        _FakeResponse(status_code=200, json_data={"success": True}),
    ]
    calls = {"n": 0}

    def fake_post(*_a, **_k):
        resp = responses[calls["n"]]
        calls["n"] += 1
        return resp

    monkeypatch.setattr(br_mod.requests, "post", fake_post)

    # 构造时跳过真实 DB 加载（避免连库）。
    monkeypatch.setattr(br_mod, "load_account_cookies", lambda *_a, **_k: {"x": "1"})
    req = BaseRequest(shop_id="shop1", user_id=1, max_retries=2, retry_delay=0.01)
    req.cookies = {"x": "1"}

    # 注入重登回调：返回新 cookies；同时 stub 掉 cookie 回写避免触库。
    relogin_calls = {"n": 0}

    def fake_relogin(**_kwargs):
        relogin_calls["n"] += 1
        return {"cookies": {"anti_content": "new-sig"}}

    monkeypatch.setattr(req, "_relogin_func", staticmethod(fake_relogin), raising=False)
    monkeypatch.setattr(sr_mod, "load_account_credentials", lambda *_a, **_k: ("u", "p"))
    monkeypatch.setattr(sr_mod, "update_account_cookies", lambda *_a, **_k: True)

    result = req.post("https://mms.pinduoduo.com/x", json_data={"a": 1})
    assert result == {"success": True}
    assert relogin_calls["n"] == 1
    assert req.cookies == {"anti_content": "new-sig"}


def test_session_expired_relogin_failure_returns_data(monkeypatch):
    """重登失败时返回过期响应体，不无限重试。"""
    expired = {"error_code": 43001, "error_msg": "会话已过期"}
    monkeypatch.setattr(
        br_mod.requests, "post", lambda *_a, **_k: _FakeResponse(json_data=expired)
    )
    monkeypatch.setattr(br_mod, "load_account_cookies", lambda *_a, **_k: {"x": "1"})

    req = BaseRequest(shop_id="shop1", user_id=1, max_retries=2, retry_delay=0.01)
    req.cookies = {"x": "1"}
    # 重登回调返回无 cookies => 失败。
    monkeypatch.setattr(
        req, "_relogin_func", staticmethod(lambda **_k: None), raising=False
    )
    monkeypatch.setattr(sr_mod, "load_account_credentials", lambda *_a, **_k: ("u", "p"))

    result = req.post("https://mms.pinduoduo.com/x", json_data={})
    assert result == expired


# ----------------------------------------------------------------------
# anti-content 签名缺失 / 失效检测（需求 26.1 / 26.2）
# ----------------------------------------------------------------------
def test_check_signature_missing_raises_before_request(monkeypatch):
    """依赖签名接口在 Cookie 缺签名时，请求前即抛 AntiContentMissingError。"""
    called = {"n": 0}
    monkeypatch.setattr(
        br_mod.requests,
        "post",
        lambda *_a, **_k: called.__setitem__("n", called["n"] + 1)
        or _FakeResponse(json_data={"success": True}),
    )

    req = _make_request(cookies={"foo": "bar"})  # 无 anti-content
    with pytest.raises(AntiContentMissingError):
        req.post("https://mms.pinduoduo.com/goods", json_data={}, check_signature=True)
    # 缺签名时不应真正发起请求。
    assert called["n"] == 0


def test_check_signature_invalid_response_raises(monkeypatch):
    """依赖签名接口返回签名失效响应时抛 AntiContentMissingError。"""
    monkeypatch.setattr(
        br_mod.requests,
        "post",
        lambda *_a, **_k: _FakeResponse(json_data={"error_msg": "anti-content 验签失败"}),
    )

    req = _make_request(cookies={"anti_content": "present"})  # 有签名但服务端判失效
    with pytest.raises(AntiContentMissingError):
        req.post("https://mms.pinduoduo.com/goods", json_data={}, check_signature=True)


def test_check_signature_valid_passes(monkeypatch):
    """携带有效签名且响应正常时，依赖签名接口正常返回。"""
    monkeypatch.setattr(
        br_mod.requests,
        "post",
        lambda *_a, **_k: _FakeResponse(json_data={"success": True, "result": "ok"}),
    )

    req = _make_request(cookies={"anti_content": "valid-sig"})
    result = req.post(
        "https://mms.pinduoduo.com/goods", json_data={}, check_signature=True
    )
    assert result == {"success": True, "result": "ok"}


def test_ensure_anti_content_raises_when_missing():
    """ensure_anti_content 在缺签名时抛异常，有签名时通过。"""
    req_missing = _make_request(cookies={})
    with pytest.raises(AntiContentMissingError):
        req_missing.ensure_anti_content()

    req_ok = _make_request(cookies={"anti-content": "sig"})
    req_ok.ensure_anti_content()  # 不应抛异常
