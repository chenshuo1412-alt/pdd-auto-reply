# -*- coding: utf-8 -*-
"""官网可见页面登录会话。和收消息、模型、发送授权分开，默认不存在。

只记录一次短时、人工确认的「打开官网登录页」许可。文件里如果夹带买家、
Cookie 或发送开关，整份会话作废。程序不会把这份会话升级成实店收消息授权。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from common.utils.time_utils import now_beijing

CONFIRM_PHRASE = "OPEN_OFFICIAL_LOGIN"
PURPOSE = "visible_login"
_MIN_TTL = 60
_MAX_TTL = 600
_FORBIDDEN = {"buyer_uids", "shop_id", "cookies", "send_enabled", "presence_enabled"}


@dataclass(frozen=True)
class LoginSession:
    """一次有效的可见页面登录许可。"""

    expires_at: datetime


def session_path() -> str:
    """会话文件路径。未配置时为空。"""
    return os.environ.get("PDD_LOGIN_SESSION_PATH", "").strip()


def _parse_expiry(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(now_beijing().tzinfo)


def read_session() -> tuple[LoginSession | None, str]:
    """每次重新读取。过期、撤销或夹带其它授权都视为无效。"""
    path = session_path()
    if not path or not os.path.isfile(path):
        return None, "没有有效的官方页面登录许可"
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None, "官方页面登录许可无法读取"
    if not isinstance(payload, dict):
        return None, "官方页面登录许可格式错误"
    if payload.get("confirmed") is not True or payload.get("purpose") != PURPOSE:
        return None, "官方页面登录尚未人工确认"
    if _FORBIDDEN.intersection(payload):
        return None, "登录许可不能携带店铺、买家或发送授权"
    expires_at = _parse_expiry(payload.get("expires_at"))
    if expires_at is None:
        return None, "登录许可缺少带时区的失效时间"
    if expires_at <= now_beijing():
        return None, "官方页面登录许可已过期"
    return LoginSession(expires_at), "ok"


def begin(confirm: str, ttl_seconds: int = 300) -> tuple[bool, str]:
    """人工确认后写一份短时许可。口令不对或时长越界则不写文件。"""
    path = session_path()
    if not path:
        return False, "未配置 PDD_LOGIN_SESSION_PATH"
    if confirm != CONFIRM_PHRASE:
        return False, "需要在管理端明确确认打开官网登录页"
    if ttl_seconds < _MIN_TTL or ttl_seconds > _MAX_TTL:
        return False, "登录许可时长必须在 1 到 10 分钟"
    expires_at = now_beijing() + timedelta(seconds=ttl_seconds)
    payload = {
        "confirmed": True,
        "purpose": PURPOSE,
        "expires_at": expires_at.isoformat(),
    }
    temporary = path + ".tmp"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    os.replace(temporary, path)
    return True, "ok"


def revoke() -> None:
    """删除许可文件。之后的页面检查和排队中的等待都会失败。"""
    path = session_path()
    if path and os.path.isfile(path):
        os.remove(path)
