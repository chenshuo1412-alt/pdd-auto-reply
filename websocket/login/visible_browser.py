# -*- coding: utf-8 -*-
"""用一块新的空目录打开官网登录页。不读取旧配置，不导出 Cookie。"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from playwright.async_api import async_playwright

from login.playwright_login import PDD_LOGIN_URL
from login.visible_login import LoginStopped, PageView

_POLL_MS = 500


class PlaywrightVisibleBrowser:
    """只使用 page.goto、page.url 和 page.title。"""

    def __init__(self) -> None:
        self._playwright = None
        self._context = None
        self._page = None
        self._directory: Path | None = None

    async def open_login(self, url: str) -> None:
        if url != PDD_LOGIN_URL:
            raise RuntimeError("只打开官网登录页")
        self._directory = Path(tempfile.mkdtemp(prefix="pdd-visible-login-"))
        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            str(self._directory),
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._page = await self._context.new_page()
        await self._page.goto(url)

    async def wait_for_leave_login(self, timeout_ms: int, still_active) -> PageView:
        if self._page is None:
            raise RuntimeError("登录页尚未打开")
        waited = 0
        while waited <= timeout_ms:
            if not still_active():
                raise LoginStopped("许可已失效")
            url = self._page.url
            title = await self._page.title()
            if "/login" not in url:
                return PageView(url, title)
            await self._page.wait_for_timeout(_POLL_MS)
            waited += _POLL_MS
        raise TimeoutError("仍停在登录页，人工验证未完成")

    async def close(self) -> None:
        if self._context is not None:
            await self._context.close()
        if self._playwright is not None:
            await self._playwright.stop()
        if self._directory is not None:
            shutil.rmtree(self._directory, ignore_errors=True)
        self._context = None
        self._playwright = None
        self._page = None
        self._directory = None
