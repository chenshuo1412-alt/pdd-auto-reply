# -*- coding: utf-8 -*-
"""平台出站策略。默认安全模式拒绝一切真实平台连接、取 token、置在线和发送。

- ``safe``：全部拒绝。未知模式也按 safe。
- ``loopback``：只允许回环地址，店铺必须在 ``PDD_ALLOW_SHOP_IDS``。
- ``live``：只允许官方主机，且必须有未过期、已人工确认、带非空测试买家白名单的授权文件。
  发送和置在线仍分别默认关闭。登录、Cookie 刷新和商品同步不因 live 放开。

本模块不预置店铺或买家。真实店铺协议未被官方授权，也未在实店验证。
"""
from __future__ import annotations

import os
from urllib.parse import urlparse

from common.core.live_grant import buyer_granted, shop_granted
from common.core.login_session import read_session

PLATFORM_HTTP_ORIGIN = "https://mms.pinduoduo.com"
PLATFORM_WS_ORIGIN = "wss://m-ws.pinduoduo.com/"
OFFICIAL_HOSTS = {"mms.pinduoduo.com", "m-ws.pinduoduo.com"}
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_TRUE = {"1", "true", "yes", "on"}
_KNOWN_MODES = {"safe", "loopback", "live"}


class PlatformDenied(RuntimeError):
    """策略拒绝平台出站。调用方不得把这次拒绝当成可重试的发送。"""


def _flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in _TRUE


def network_mode() -> str:
    """当前出站模式。未知值失败关闭，按 safe 处理。"""
    mode = os.environ.get("PDD_NETWORK_MODE", "safe").strip().lower()
    if mode not in _KNOWN_MODES:
        return "safe"
    return mode


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().strip("[]")


def is_loopback_url(url: str) -> bool:
    """URL 主机是否为回环地址。"""
    return _host(url) in _LOOPBACK


def _allow_shop(shop_id: str | None) -> bool:
    allowed = {item.strip() for item in os.environ.get("PDD_ALLOW_SHOP_IDS", "").split(",") if item.strip()}
    return bool(shop_id) and str(shop_id) in allowed


def _allow_session(customer_uid: str | None) -> bool:
    raw = os.environ.get("PDD_ALLOW_SESSION_IDS", "")
    allowed = {item.strip() for item in raw.split(",") if item.strip()}
    if not allowed:
        return True
    return customer_uid is not None and str(customer_uid) in allowed


def _official_url(url: str) -> bool:
    return _host(url) in OFFICIAL_HOSTS and urlparse(url).username is None


def http_origin() -> str:
    """平台 HTTP 源。只有 loopback 模式才接受回环覆盖。live 固定官方源。"""
    if network_mode() == "loopback":
        override = os.environ.get("PDD_HTTP_BASE_URL", "").strip().rstrip("/")
        if override.startswith("http://") and is_loopback_url(override):
            return override
    return PLATFORM_HTTP_ORIGIN


def ws_origin() -> str:
    """平台 WebSocket 源。只有 loopback 模式才接受回环覆盖。"""
    if network_mode() == "loopback":
        override = os.environ.get("PDD_WS_BASE_URL", "").strip()
        if override.startswith("ws://") and is_loopback_url(override):
            return override if override.endswith("/") else override + "/"
    return PLATFORM_WS_ORIGIN


def rewrite_platform_http(url: str) -> str:
    """在 loopback 覆盖有效时，把官方 HTTP 源换成回环源。"""
    origin = http_origin()
    if origin != PLATFORM_HTTP_ORIGIN and url.startswith(PLATFORM_HTTP_ORIGIN):
        return origin + url[len(PLATFORM_HTTP_ORIGIN):]
    return url


def decide(
    purpose: str,
    shop_id: str | None = None,
    url: str | None = None,
    customer_uid: str | None = None,
) -> tuple[bool, str]:
    """判断一次平台动作是否允许。返回 (允许, 中文原因)。"""
    if purpose == "visible_login":
        return _decide_visible_login(url)
    mode = network_mode()
    if mode == "safe":
        return False, "安全模式拒绝平台出站"
    if purpose == "login":
        return False, "禁止打开平台登录页"
    if purpose == "scheduler":
        if mode != "loopback" or not _flag("PDD_SCHEDULER_PLATFORM"):
            return False, "调度不因接入模式放开"
        return True, "ok"
    if mode == "loopback":
        return _decide_loopback(purpose, shop_id, url, customer_uid)
    return _decide_live(purpose, shop_id, url, customer_uid)


def _decide_visible_login(url: str | None) -> tuple[bool, str]:
    """只放行短时许可下的官网登录页或首页。不授权收消息或发送。"""
    if url is not None:
        parsed = urlparse(url)
        path = (parsed.path or "/").rstrip("/") or "/"
        if (
            parsed.scheme != "https"
            or (parsed.hostname or "").lower() != "mms.pinduoduo.com"
            or parsed.username is not None
            or path not in {"/login", "/home"}
        ):
            return False, "只允许打开官网登录页或首页"
    active, reason = read_session()
    if active is None:
        return False, reason
    return True, "ok"


def _decide_loopback(
    purpose: str,
    shop_id: str | None,
    url: str | None,
    customer_uid: str | None,
) -> tuple[bool, str]:
    if url is not None and not is_loopback_url(url):
        return False, "非回环地址拒绝"
    if not _allow_shop(shop_id):
        return False, "店铺不在允许列表"
    if purpose == "presence" and not _flag("PDD_PRESENCE_ENABLED"):
        return False, "置在线开关关闭"
    if purpose == "send":
        if not _flag("PDD_SEND_ENABLED"):
            return False, "发送开关关闭"
        if not _allow_session(customer_uid):
            return False, "会话不在允许列表"
    if purpose not in {"connect", "token", "read", "presence", "send"}:
        return False, "未知动作拒绝"
    return True, "ok"


def _decide_live(
    purpose: str,
    shop_id: str | None,
    url: str | None,
    customer_uid: str | None,
) -> tuple[bool, str]:
    if url is not None and not _official_url(url):
        return False, "未知地址拒绝"
    allowed, reason = shop_granted(shop_id)
    if not allowed:
        return False, reason
    if purpose == "presence":
        if not _flag("PDD_PRESENCE_ENABLED"):
            return False, "置在线开关关闭"
        return True, "ok"
    if purpose == "send":
        if not _flag("PDD_SEND_ENABLED"):
            return False, "发送开关关闭"
        buyer_ok, buyer_reason = buyer_granted(shop_id, customer_uid)
        if not buyer_ok:
            return False, buyer_reason
        return True, "ok"
    if purpose in {"connect", "token", "read"}:
        return True, "ok"
    return False, "未知动作拒绝"


def enforce(
    purpose: str,
    shop_id: str | None = None,
    url: str | None = None,
    customer_uid: str | None = None,
) -> None:
    """不允许时抛 PlatformDenied。"""
    allowed, reason = decide(purpose, shop_id=shop_id, url=url, customer_uid=customer_uid)
    if not allowed:
        raise PlatformDenied(reason)


def scheduler_platform_enabled() -> bool:
    """定时任务是否可以触发平台刷新/同步。默认否。"""
    allowed, _reason = decide("scheduler")
    return allowed
