# -*- coding: utf-8 -*-
"""出站回复去重表。同一店铺的同一 msg_id 只允许一次发送尝试。"""
from __future__ import annotations

from sqlalchemy import String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from common.models.base import AuditMixin, Base


class ReplyDispatch(AuditMixin, Base):
    """平台回复派发记录。

    ``state``：
    - claimed：已占用，尚未确认结果（含进程崩溃）
    - sent：对端明确接受
    - failed：明确失败或策略拒绝，不自动重试
    - unknown：超时、断连或空响应，可能已送达，禁止自动重试
    """

    __tablename__ = "pdd_reply_dispatch"
    __table_args__ = (
        UniqueConstraint("shop_id", "msg_id", name="uix_pdd_reply_dispatch_shop_msg"),
        {"comment": "出站回复去重（店铺 ID + 消息 ID）"},
    )

    shop_id: Mapped[str] = mapped_column(String(128), nullable=False, comment="店铺业务 ID")
    msg_id: Mapped[str] = mapped_column(String(128), nullable=False, comment="平台消息 ID")
    customer_uid: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="客户 UID")
    state: Mapped[str] = mapped_column(String(16), nullable=False, comment="claimed/sent/failed/unknown")
    detail: Mapped[str | None] = mapped_column(Text, nullable=True, comment="结果说明，不含原文全文")
