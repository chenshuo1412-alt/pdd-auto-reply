# -*- coding: utf-8 -*-
"""官方可见登录与测试买家配对边界。不连接真实平台。"""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path

import pytest

from common.core.login_session import begin, read_session, revoke
from common.core.platform_guard import decide
from common.utils.time_utils import now_beijing
from login.visible_login import (
    OFFICIAL_LOGIN_URL,
    LoginStopped,
    PageView,
    run_visible_login,
)


class FakeBrowser:
    """不提供 Cookie 方法，只回放测试页面。"""

    def __init__(self, view: PageView | Exception):
        self.view = view
        self.opened = []
        self.closed = False

    async def open_login(self, url: str) -> None:
        self.opened.append(url)

    async def wait_for_leave_login(self, timeout_ms: int, still_active) -> PageView:
        if not still_active():
            raise LoginStopped("revoked")
        if isinstance(self.view, Exception):
            raise self.view
        if not still_active():
            raise LoginStopped("revoked")
        return self.view

    async def close(self) -> None:
        self.closed = True

    def cookies(self):
        raise AssertionError("visible login must not export cookies")


def _session_env(monkeypatch, path: Path):
    monkeypatch.setenv("PDD_LOGIN_SESSION_PATH", str(path))
    monkeypatch.setenv("PDD_NETWORK_MODE", "safe")
    monkeypatch.setenv("PDD_SEND_ENABLED", "0")
    monkeypatch.setenv("PDD_PRESENCE_ENABLED", "0")


def test_unconfirmed_login_does_not_open_browser(tmp_path, monkeypatch):
    _session_env(monkeypatch, tmp_path / "login.json")
    browser = FakeBrowser(PageView("https://mms.pinduoduo.com/home/", "首页"))
    result = asyncio.run(run_visible_login(browser, confirm="nope"))
    assert result.status == "refused"
    assert browser.opened == []
    assert not (tmp_path / "login.json").exists()
    assert decide("send", shop_id="shop-a", customer_uid="buyer-a")[0] is False


def test_visible_page_reaches_home_without_ids_or_cookies(tmp_path, monkeypatch):
    _session_env(monkeypatch, tmp_path / "login.json")
    browser = FakeBrowser(PageView("https://mms.pinduoduo.com/home/", "首页"))
    result = asyncio.run(
        run_visible_login(browser, confirm="OPEN_OFFICIAL_LOGIN", timeout_ms=50)
    )
    assert browser.opened == [OFFICIAL_LOGIN_URL]
    assert browser.closed is True
    assert result.status == "visible_page_reached"
    assert result.shop_id is None
    assert result.buyer_uid is None
    assert result.cookies_exported is False
    assert result.can_connect is False
    assert result.limitation
    assert "买家" in result.limitation
    raw = json.loads((tmp_path / "login.json").read_text(encoding="utf-8"))
    assert "cookies" not in raw
    assert "buyer_uids" not in raw
    assert decide("connect", shop_id="shop-a", url="wss://m-ws.pinduoduo.com/")[0] is False


def test_captcha_timeout_stays_failed(tmp_path, monkeypatch):
    _session_env(monkeypatch, tmp_path / "login.json")
    browser = FakeBrowser(PageView("https://mms.pinduoduo.com/login", "登录"))
    result = asyncio.run(run_visible_login(browser, confirm="OPEN_OFFICIAL_LOGIN"))
    assert result.status == "failed"
    assert result.can_connect is False
    assert browser.closed is True


def test_offsite_redirect_is_rejected(tmp_path, monkeypatch):
    _session_env(monkeypatch, tmp_path / "login.json")
    browser = FakeBrowser(PageView("https://evil.example/home", "首页"))
    result = asyncio.run(run_visible_login(browser, confirm="OPEN_OFFICIAL_LOGIN"))
    assert result.status == "failed"
    assert result.shop_id is None


def test_revoke_and_smuggled_fields_stop_the_flow(tmp_path, monkeypatch):
    path = tmp_path / "login.json"
    _session_env(monkeypatch, path)
    assert begin("OPEN_OFFICIAL_LOGIN", 120)[0] is True
    revoke()
    browser = FakeBrowser(PageView("https://mms.pinduoduo.com/home/", "首页"))
    result = asyncio.run(run_visible_login(browser))
    assert result.status == "refused"
    assert browser.opened == []

    path.write_text(
        json.dumps(
            {
                "confirmed": True,
                "purpose": "visible_login",
                "expires_at": (now_beijing() + timedelta(minutes=5)).isoformat(),
                "buyer_uids": ["buyer-a"],
                "cookies": {"PASS_ID": "synthetic"},
            }
        ),
        encoding="utf-8",
    )
    assert read_session()[0] is None
    assert asyncio.run(run_visible_login(browser)).status == "refused"


def test_expired_session_has_no_effect(tmp_path, monkeypatch):
    path = tmp_path / "login.json"
    _session_env(monkeypatch, path)
    path.write_text(
        json.dumps(
            {
                "confirmed": True,
                "purpose": "visible_login",
                "expires_at": (now_beijing() - timedelta(minutes=1)).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    browser = FakeBrowser(PageView("https://mms.pinduoduo.com/home/", "首页"))
    assert asyncio.run(run_visible_login(browser)).status == "refused"
    assert browser.opened == []
    assert decide("visible_login", url="http://127.0.0.1/login")[0] is False
