# -*- coding: utf-8 -*-
"""
websocket.routes.messages —— 手动发送消息接口（供 backend 调用）
==============================================================
本文件用途：提供 websocket 服务的「手动发送消息」HTTP 接口，供 backend 服务在
「在线聊天 - 手动发送消息」时经服务间 HTTP 调用，将客服消息下发至对应客户会话
（需求 14.3）。

- ``POST /messages/send``：按 (shop_id, owner_user_id) 复用拼多多消息发送接口
  （``channel_pdd.api.send_message.SendMessage.send_text``）下发一条文本消息。

接口约定（开发规范 1-3）：HTTP 恒返回 200，业务成败由统一响应体
``{code, success, message, data}`` 表达；发送成功 / 失败均由 backend 据响应记录
消息日志。地址经环境变量配置（禁止写死 localhost，规范 21）。本路由不抛异常，
失败统一规整为失败响应（健壮性兜底，需求 26）。

实现约束（开发规范）：导入置顶（规范 51）、中文注释（规范 37）、文件名用下划线
（规范 40）、单文件 ≤500 行（规范 35）、日志禁用 debug（规范 38）。
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from channel_pdd.api.get_chat_history import GetChatHistory
from channel_pdd.api.get_conversations import GetConversations
from channel_pdd.api.send_message import SendMessage
from common.core.platform_guard import decide
from common.schemas.common import ApiResponse, error_response, success_response

logger = logging.getLogger("websocket.routes.messages")

# 手动发送消息路由：标签便于 OpenAPI 分组；前缀由聚合层添加。
router = APIRouter(tags=["消息发送"])


class SendMessageRequest(BaseModel):
    """手动发送消息请求体（与 backend chat_send_client 约定一致）。"""

    shop_pk: int = Field(..., description="店铺主键 shop.id")
    shop_id: str = Field(..., description="拼多多店铺业务标识")
    owner_user_id: Optional[int] = Field(None, description="店铺归属用户 ID")
    recipient_uid: str = Field(..., description="接收消息的客户唯一标识")
    content: str = Field(..., description="待发送的文本内容")


@router.post(
    "/messages/send",
    response_model=ApiResponse,
    summary="向指定客户会话手动发送一条文本消息",
)
async def send_message(payload: SendMessageRequest) -> ApiResponse:
    """向指定客户会话手动发送一条文本消息（需求 14.3）。

    复用 ``SendMessage`` 按 (shop_id, owner_user_id) 加载 Cookie 并经拼多多接口下发
    文本消息。下发成功返回成功响应；失败返回失败响应（由 backend 据此记消息日志）。
    本路由不抛异常（健壮性兜底）。

    Args:
        payload: 含 shop_pk / shop_id / owner_user_id / recipient_uid / content 的请求体。

    Returns:
        统一响应体：下发成功返回 success；失败返回 error_response。
    """
    allowed, reason = decide(
        "send", shop_id=payload.shop_id, customer_uid=str(payload.recipient_uid)
    )
    if not allowed:
        return error_response(-1, reason)
    try:
        sender = SendMessage(shop_id=payload.shop_id, user_id=payload.owner_user_id)
        result = sender.send_text(payload.recipient_uid, payload.content)
    except Exception as exc:  # noqa: BLE001 - 发送异常不抛出，规整为失败响应
        logger.error("手动发送消息异常: shop_id=%s, %s", payload.shop_id, exc)
        return error_response(-1, "消息发送未确认，请勿立即重发")

    if getattr(result, "status", None) == "sent":
        logger.info(
            "手动消息已下发: shop_id=%s, customer=%s",
            payload.shop_id,
            payload.recipient_uid,
        )
        return success_response(message="消息已发送")

    if getattr(result, "status", None) == "unknown":
        return error_response(-1, "发送结果不明，已禁止自动重试")
    logger.warning("手动发送消息失败: shop_id=%s", payload.shop_id)
    return error_response(-1, "消息发送失败，未自动重试")


class ConversationListRequest(BaseModel):
    """会话列表查询请求体（在线聊天左侧列表，方案 A 实时调拼多多接口）。"""

    shop_id: str = Field(..., description="拼多多店铺业务标识")
    owner_user_id: Optional[int] = Field(None, description="店铺归属用户 ID")
    size: int = Field(100, description="每页拉取条数（拼多多侧分页）")
    fetch_all: bool = Field(False, description="是否翻页拉取全部会话")


class ChatHistoryRequest(BaseModel):
    """会话历史聊天记录查询请求体（在线聊天右侧聊天窗）。"""

    shop_pk: int = Field(..., description="本地店铺主键 shop.id（落库用）")
    shop_id: str = Field(..., description="拼多多店铺业务标识")
    owner_user_id: Optional[int] = Field(None, description="店铺归属用户 ID")
    customer_uid: str = Field(..., description="客户唯一标识")
    size: int = Field(50, description="每页拉取条数（拼多多侧分页）")
    persist: bool = Field(True, description="是否将历史记录落库到本地表")


@router.post(
    "/messages/conversations",
    response_model=ApiResponse,
    summary="拉取店铺最近会话列表（在线聊天左侧列表）",
)
async def list_conversations(payload: ConversationListRequest) -> ApiResponse:
    """拉取店铺最近会话列表（方案 A：实时调拼多多接口，需求 14）。

    复用 ``GetConversations`` 按 (shop_id, owner_user_id) 加载 Cookie 后拉取拼多多
    最近会话列表并规整为统一字典；本路由不抛异常（健壮性兜底）。

    Args:
        payload: 含 shop_id / owner_user_id / size / fetch_all 的请求体。

    Returns:
        统一响应体：成功返回 {conversations: [...]}；失败返回 error_response。
    """
    try:
        fetcher = GetConversations(
            shop_id=payload.shop_id, user_id=payload.owner_user_id
        )
        if payload.fetch_all:
            result = fetcher.fetch_all(size=payload.size)
        else:
            result = fetcher.fetch(page=1, size=payload.size)
    except Exception as exc:  # noqa: BLE001 - 拉取异常不抛出，规整为失败响应
        logger.error("拉取会话列表异常: shop_id=%s, %s", payload.shop_id, exc)
        return error_response(-1, "获取会话列表失败，请稍后重试")

    if result.get("success"):
        return success_response(
            data={"conversations": result.get("conversations", [])},
            message="获取成功",
        )
    return error_response(-1, result.get("error_msg") or "获取会话列表失败")


@router.post(
    "/messages/history",
    response_model=ApiResponse,
    summary="拉取某会话的全部历史聊天记录（在线聊天右侧聊天窗）",
)
async def chat_history(payload: ChatHistoryRequest) -> ApiResponse:
    """拉取某客户会话的全部历史聊天记录并可选落库（方案 A，需求 14 / 17）。

    复用 ``GetChatHistory`` 循环翻页拉取该会话「接口支持范围内」的全部历史消息；
    ``persist=True`` 时按 (shop_pk, customer_uid, msg_id) 去重落库到本地表。本路由
    不抛异常（健壮性兜底）。

    Args:
        payload: 含 shop_pk / shop_id / owner_user_id / customer_uid / size / persist。

    Returns:
        统一响应体：成功返回 {messages: [...], persisted: n}；失败返回 error_response。
    """
    try:
        fetcher = GetChatHistory(
            shop_id=payload.shop_id, user_id=payload.owner_user_id
        )
        if payload.persist:
            result = fetcher.fetch_all_and_persist(
                shop_pk=payload.shop_pk,
                customer_uid=payload.customer_uid,
                size=payload.size,
            )
        else:
            result = fetcher.fetch_all(
                customer_uid=payload.customer_uid, size=payload.size
            )
    except Exception as exc:  # noqa: BLE001 - 拉取异常不抛出，规整为失败响应
        logger.error(
            "拉取聊天记录异常: shop_id=%s, customer=%s, %s",
            payload.shop_id, payload.customer_uid, exc,
        )
        return error_response(-1, "获取聊天记录失败，请稍后重试")

    if result.get("success"):
        return success_response(
            data={
                "messages": result.get("messages", []),
                "persisted": result.get("persisted", 0),
            },
            message="获取成功",
        )
    return error_response(-1, result.get("error_msg") or "获取聊天记录失败")


__all__ = ["router"]
