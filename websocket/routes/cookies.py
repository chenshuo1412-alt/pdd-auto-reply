# -*- coding: utf-8 -*-
"""
websocket.routes.cookies —— Cookie 刷新接口（供 scheduler 调用）
==============================================================
本文件用途：提供 websocket 服务的「Cookie 刷新」HTTP 接口，供 scheduler 定时任务
服务经服务间 HTTP 调用，定时刷新指定店铺账号的登录态 Cookie（需求 4.6 / 21.2）。

- ``POST /cookies/refresh``：按 (shop_id, owner_user_id) 定位账号名后，复用
  ``channel_pdd.pdd_login.refresh_pdd_cookies`` 无头刷新 Cookie 并维护登录态；
  刷新成功后将新 Cookie 以可逆加密回写 account 表（不返回明文，需求 3.6）。

接口约定（开发规范 1-3）：HTTP 恒返回 200，业务成败由统一响应体
``{code, success, message, data}`` 表达。地址经环境变量配置（禁止写死 localhost，
规范 21）。本路由不抛异常，失败统一规整为失败响应（健壮性兜底，需求 26）；
对外响应不包含 Cookie 等敏感字段（需求 3.6）。

实现约束（开发规范）：导入置顶（规范 51）、中文注释（规范 37）、文件名用下划线
（规范 40）、单文件 ≤500 行（规范 35）、日志禁用 debug（规范 38）、复用共通（规范 36/52）。
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from channel_pdd import pdd_login
from channel_pdd.core.credential_store import (
    load_account_credentials,
    update_account_cookies,
)
from common.core.platform_guard import scheduler_platform_enabled
from common.schemas.common import ApiResponse, error_response, success_response

logger = logging.getLogger("websocket.routes.cookies")

# Cookie 刷新路由：标签便于 OpenAPI 分组；前缀由聚合层添加。
router = APIRouter(tags=["Cookie 刷新"])


class RefreshCookieRequest(BaseModel):
    """Cookie 刷新请求体（与 scheduler service_client 约定一致）。"""

    shop_pk: int = Field(..., description="店铺主键 shop.id")
    shop_id: str = Field(..., description="拼多多店铺业务标识")
    owner_user_id: Optional[int] = Field(None, description="店铺归属用户 ID")


@router.post(
    "/cookies/refresh",
    response_model=ApiResponse,
    summary="刷新指定店铺账号的登录态 Cookie",
)
async def refresh_cookie(payload: RefreshCookieRequest) -> ApiResponse:
    """刷新指定店铺账号的登录态 Cookie（需求 4.6 / 21.2）。

    先按 (shop_id, owner_user_id) 定位账号名，再复用 ``refresh_pdd_cookies`` 无头
    刷新 Cookie 并维护登录态；刷新成功后将新 Cookie 加密回写 account 表。无论刷新
    成败本路由都不抛异常：成功返回 success；登录态失效 / 失败返回 error_response。
    对外响应不包含 Cookie 明文（需求 3.6）。

    Args:
        payload: 含 shop_pk / shop_id / owner_user_id 的刷新请求体。

    Returns:
        统一响应体：刷新成功返回 success；失败返回 error_response。
    """
    # owner_user_id 缺失无法定位账号凭据，直接返回失败（不抛异常）。
    if not scheduler_platform_enabled():
        return error_response(-1, "安全模式拒绝刷新平台会话")
    if payload.owner_user_id is None:
        logger.warning("Cookie 刷新缺少归属用户 ID：shop_id=%s", payload.shop_id)
        return error_response(-1, "缺少归属用户信息，无法刷新 Cookie")

    # 按 (shop_id, owner_user_id) 定位账号名（复用共通凭据适配层）。
    credentials = load_account_credentials(payload.shop_id, payload.owner_user_id)
    if not credentials or not credentials[0]:
        logger.warning(
            "Cookie 刷新未找到账号凭据：shop_id=%s, user_id=%s",
            payload.shop_id,
            payload.owner_user_id,
        )
        return error_response(-1, "未找到账号凭据，无法刷新 Cookie")

    username = credentials[0]
    try:
        info = await pdd_login.refresh_pdd_cookies(username, payload.owner_user_id)
    except Exception as exc:  # noqa: BLE001 - 刷新异常不抛出，规整为失败响应
        logger.error("Cookie 刷新异常：shop_id=%s, %s", payload.shop_id, exc)
        return error_response(-1, "Cookie 刷新失败，请稍后重试")

    if info is None:
        logger.warning("店铺 shop_id=%s 登录态已失效，Cookie 刷新失败", payload.shop_id)
        return error_response(-1, "登录态已失效，需重新登录")

    # 刷新成功：将新 Cookie 加密回写 account 表（不返回明文，需求 3.6）。
    update_account_cookies(
        payload.shop_id, payload.owner_user_id, info.get("cookies")
    )
    logger.info("店铺 shop_id=%s Cookie 刷新成功", payload.shop_id)
    # 仅返回非敏感的店铺标识信息，绝不外泄 Cookie 明文。
    return success_response(
        data={"shop_id": payload.shop_id, "shop_pk": payload.shop_pk},
        message="Cookie 刷新成功",
    )


__all__ = ["router"]
