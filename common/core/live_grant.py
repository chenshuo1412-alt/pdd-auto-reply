# -*- coding: utf-8 -*-
"""人工确认的实店接入授权。默认不存在，代码不预置任何店铺或买家。

授权文件由操作者自行编写，路径只来自环境变量 ``PDD_LIVE_GRANT_PATH``。
每次判断都重新读取，撤销、过期和改名单立即生效。买家只认稳定 uid，
不认昵称、顺序或通配符。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from common.utils.time_utils import now_beijing

_WILDCARD = {"*", "all", "any", "?", ""}


@dataclass(frozen=True)
class LiveGrant:
    """一份已通过校验的授权。"""

    shop_id: str
    buyer_uids: frozenset[str]
    expires_at: datetime


def grant_path() -> str:
    """当前授权文件路径。未配置时为空。"""
    return os.environ.get("PDD_LIVE_GRANT_PATH", "").strip()


def stable_buyer_uid(value: Any) -> str | None:
    """只接受明确的买家 uid 字符串。昵称、空白、通配和歧义值都拒绝。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.lower() in _WILDCARD:
        return None
    if any(char.isspace() for char in text):
        return None
    if any(char in text for char in "*?，,"):
        return None
    return text


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


def read_grant() -> tuple[LiveGrant | None, str]:
    """读取并校验授权。失败时没有可用授权。"""
    path = grant_path()
    if not path:
        return None, "未配置人工确认的接入授权"
    if not os.path.isfile(path):
        return None, "接入授权不存在或已撤销"
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None, "接入授权无法读取"
    if not isinstance(payload, dict):
        return None, "接入授权格式错误"
    if payload.get("confirmed") is not True:
        return None, "接入尚未人工确认"
    shop_id = stable_buyer_uid(payload.get("shop_id"))
    if shop_id is None:
        return None, "授权店铺标识无效"
    raw_buyers = payload.get("buyer_uids")
    if not isinstance(raw_buyers, list) or not raw_buyers:
        return None, "测试买家白名单不能为空"
    buyers: set[str] = set()
    for item in raw_buyers:
        uid = stable_buyer_uid(item)
        if uid is None:
            return None, "测试买家白名单含无效或通配标识"
        buyers.add(uid)
    expires_at = _parse_expiry(payload.get("expires_at"))
    if expires_at is None:
        return None, "授权缺少带时区的失效时间"
    if expires_at <= now_beijing():
        return None, "接入授权已过期"
    return LiveGrant(shop_id, frozenset(buyers), expires_at), "ok"


def shop_granted(shop_id: str | None) -> tuple[bool, str]:
    """店铺是否在当前有效授权内。白名单为空的文件不会通过。"""
    grant, reason = read_grant()
    if grant is None:
        return False, reason
    if stable_buyer_uid(shop_id) != grant.shop_id:
        return False, "店铺不在人工确认的授权范围"
    return True, "ok"


def buyer_granted(shop_id: str | None, buyer_uid: Any) -> tuple[bool, str]:
    """买家 uid 是否属于该店铺的测试白名单。"""
    grant, reason = read_grant()
    if grant is None:
        return False, reason
    if stable_buyer_uid(shop_id) != grant.shop_id:
        return False, "店铺不在人工确认的授权范围"
    uid = stable_buyer_uid(buyer_uid)
    if uid is None:
        return False, "买家身份不明，已拒绝"
    if uid not in grant.buyer_uids:
        return False, "买家不在测试白名单"
    return True, "ok"


def screen_inbound(shop_id: str | None, buyer_uid: Any, *, mode: str) -> tuple[bool, str]:
    """实店模式下，非白名单消息不得进入后续处理。其它模式不在这里拦截。"""
    if mode != "live":
        return True, "ok"
    return buyer_granted(shop_id, buyer_uid)
