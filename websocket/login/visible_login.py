# -*- coding: utf-8 -*-
"""只打开官网可见登录页，等待人工完成。不导出 Cookie，不调用店铺接口。

可见页面目前不能提供可核对的店铺业务 ID 或买家 uid。因此本流程成功也只表示
浏览器留在官网登录后的页面上，不会创建收消息授权，不会置在线或发送。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol
from urllib.parse import urlparse

from common.core.login_session import begin, read_session
from common.core.platform_guard import PlatformDenied, enforce

OFFICIAL_LOGIN_URL = "https://mms.pinduoduo.com/login"
_SUCCESS_TITLES = ("拼多多 商家后台", "首页", "订单查询")
_LIMITATION = (
    "官网这个页面没有显示可核对的店铺业务 ID 或买家 uid。"
    "本程序没有保存 Cookie，也不能据此收消息或确认测试买家。"
    "验证码未通过时登录会失败，不会绕过。"
)


class LoginStopped(RuntimeError):
    """许可在等待过程中被撤销或过期。"""


@dataclass(frozen=True)
class PageView:
    """浏览器当前可见的地址和标题。不含 Cookie。"""

    url: str
    title: str


class VisibleBrowser(Protocol):
    """可见登录使用的浏览器。实现不得提供 Cookie 导出。"""

    async def open_login(self, url: str) -> None:
        """打开给定地址。"""

    async def wait_for_leave_login(self, timeout_ms: int, still_active) -> PageView:
        """等到离开登录页，或在许可失效时抛 LoginStopped。"""

    async def close(self) -> None:
        """关闭浏览器。"""


@dataclass
class VisibleLoginResult:
    """官方页面核对结果。店铺 ID 与买家 uid 固定为空。"""

    status: str
    message: str
    title: Optional[str] = None
    final_url: Optional[str] = None
    shop_id: None = None
    buyer_uid: None = None
    cookies_exported: bool = False
    can_connect: bool = False
    limitation: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "message": self.message,
            "title": self.title,
            "final_url": self.final_url,
            "shop_id": None,
            "buyer_uid": None,
            "cookies_exported": False,
            "can_connect": False,
            "limitation": self.limitation,
        }


def _official_visible(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    return (
        (parsed.hostname or "").lower() == "mms.pinduoduo.com"
        and parsed.username is None
        and path in {"/login", "/home"}
        and parsed.scheme == "https"
    )


async def run_visible_login(
    browser: VisibleBrowser,
    *,
    confirm: Optional[str] = None,
    ttl_seconds: int = 300,
    timeout_ms: int = 1_000,
) -> VisibleLoginResult:
    """在有效许可内打开官网登录页。confirm 给出时先写短时许可。"""
    if confirm is not None:
        ok, reason = begin(confirm, ttl_seconds)
        if not ok:
            return VisibleLoginResult("refused", reason)
    active, reason = read_session()
    if active is None:
        return VisibleLoginResult("refused", reason)
    try:
        enforce("visible_login", url=OFFICIAL_LOGIN_URL)
        await browser.open_login(OFFICIAL_LOGIN_URL)
        active, reason = read_session()
        if active is None:
            return VisibleLoginResult("refused", reason)
        view = await browser.wait_for_leave_login(
            timeout_ms, lambda: read_session()[0] is not None
        )
    except LoginStopped:
        return VisibleLoginResult("refused", "官方页面登录许可已撤销或过期")
    except PlatformDenied as exc:
        return VisibleLoginResult("refused", str(exc))
    except Exception as exc:  # noqa: BLE001 - 超时和页面失败都保持失败
        return VisibleLoginResult("failed", f"官方页面登录未完成：{exc}")
    finally:
        await browser.close()
    active, reason = read_session()
    if active is None:
        return VisibleLoginResult("refused", reason)
    if not _official_visible(view.url) or urlparse(view.url).path.rstrip("/") == "/login":
        return VisibleLoginResult(
            "failed",
            "仍停在登录页，或页面离开了官网。验证码未完成时不会继续。",
            title=view.title,
            final_url=view.url,
        )
    if view.title not in _SUCCESS_TITLES:
        return VisibleLoginResult(
            "failed",
            "官网页面标题无法确认已进入商家后台。",
            title=view.title,
            final_url=view.url,
        )
    return VisibleLoginResult(
        "visible_page_reached",
        "已打开并停留在官网商家后台可见页面。没有取得店铺 ID 或买家 uid。",
        title=view.title,
        final_url=view.url,
        limitation=_LIMITATION,
    )
