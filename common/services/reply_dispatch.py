# -*- coding: utf-8 -*-
"""出站去重。存储失败时拒绝发送，避免重复回复。"""
from __future__ import annotations

import logging

from sqlalchemy.exc import IntegrityError

from common.db.repository import Repository
from common.db.session import session_scope
from common.models.reply_dispatch import ReplyDispatch

logger = logging.getLogger("common.reply_dispatch")

_TERMINAL = {"sent", "failed", "unknown", "claimed"}


def admit(shop_id: str, msg_id: str, customer_uid: str | None = None) -> str:
    """占用一条出站。返回 owner 表示本次可以发送；其它值表示必须跳过。

    已存在的 claimed 也跳过：进程可能在发送后、落账前退出，结果视为不明。
    """
    text_id = str(msg_id or "").strip()
    if not text_id:
        return "missing_msg_id"
    try:
        with session_scope() as session:
            try:
                Repository(ReplyDispatch, session).create(
                    shop_id=str(shop_id),
                    msg_id=text_id,
                    customer_uid=None if customer_uid is None else str(customer_uid),
                    state="claimed",
                    detail="",
                )
                return "owner"
            except IntegrityError:
                session.rollback()
                row = Repository(ReplyDispatch, session).get_by(
                    shop_id=str(shop_id), msg_id=text_id
                )
                state = row.state if row is not None else "claimed"
                return state if state in _TERMINAL else "claimed"
    except Exception as exc:  # noqa: BLE001 - 去重库不可用时禁止发送
        logger.error("出站去重不可用，拒绝发送: shop_id=%s, %s", shop_id, exc)
        return "store_unavailable"


def finish(shop_id: str, msg_id: str, state: str, detail: str = "") -> None:
    """把 claimed 记成终态。已是终态的记录不覆盖。"""
    if state not in {"sent", "failed", "unknown"}:
        state = "unknown"
    try:
        with session_scope() as session:
            row = Repository(ReplyDispatch, session).get_by(
                shop_id=str(shop_id), msg_id=str(msg_id)
            )
            if row is None or row.state != "claimed":
                return
            Repository(ReplyDispatch, session).update(
                row.id, state=state, detail=(detail or "")[:500]
            )
    except Exception as exc:  # noqa: BLE001
        logger.error("写出站结果失败: shop_id=%s, msg_id=%s, %s", shop_id, msg_id, exc)
