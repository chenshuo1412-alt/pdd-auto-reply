# -*- coding: utf-8 -*-
"""平台出站策略。默认安全模式拒绝一切真实平台连接、取 token、置在线和发送。

只读/回环试验必须同时满足：
- ``PDD_NETWORK_MODE=loopback``
- 目标主机是 127.0.0.1、::1 或 localhost
- ``PDD_ALLOW_SHOP_IDS`` 含该店铺
发送还要 ``PDD_SEND_ENABLED=1``，以及可选的 ``PDD_ALLOW_SESSION_IDS``。
置在线还要 ``PDD_PRESENCE_ENABLED=1``。调度触发平台任务还要 ``PDD_SCHEDULER_PLATFORM=1``。

非回环地址（含拼多多官方域名）在任何模式下都拒绝。``live`` 等未知模式按 safe 处理。
登录浏览器始终拒绝，避免这轮误开真实登录页。
"""
from __future__ import annotations

import os
from urllib.parse import urlparse

PLATFORM_HTTP_ORIGIN = "https://mms.pinduoduo.com"
PLATFORM_WS_ORIGIN = "wss://m-ws.pinduoduo.com/"
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_TRUE = {"1", "true", "yes", "on"}


class PlatformDenied(RuntimeError):
    """策略拒绝平台出站。调用方不得把这次拒绝当成可重试的发送。"""


def _flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in _TRUE


def network_mode() -> str:
    """当前出站模式。只有显式 loopback 离开安全模式。"""
    mode = os.environ.get("PDD_NETWORK_MODE", "safe").strip().lower()
    return "loopback" if mode == "loopback" else "safe"


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


def http_origin() -> str:
    """平台 HTTP 源。只有 loopback 模式才接受回环覆盖，否则保持官方源（随后会被拒绝）。"""
    if network_mode() != "loopback":
        return PLATFORM_HTTP_ORIGIN
    override = os.environ.get("PDD_HTTP_BASE_URL", "").strip().rstrip("/")
    if override.startswith("http://") and is_loopback_url(override):
        return override
    return PLATFORM_HTTP_ORIGIN


def ws_origin() -> str:
    """平台 WebSocket 源。规则与 http_origin 相同。"""
    if network_mode() != "loopback":
        return PLATFORM_WS_ORIGIN
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
    if network_mode() != "loopback":
        return False, "安全模式拒绝平台出站"
    if purpose == "login":
        return False, "禁止打开平台登录页"
    if purpose == "scheduler":
        if not _flag("PDD_SCHEDULER_PLATFORM"):
            return False, "安全模式跳过平台调度"
        return True, "ok"
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
    return True, "ok"


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
