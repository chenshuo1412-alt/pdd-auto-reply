# -*- coding: utf-8 -*-
"""
websocket.routes.login —— 店铺登录接口（供 backend 调用）
========================================================
本文件用途：提供 websocket 服务的「账号密码登录 / Cookie 导入」HTTP 接口，供
backend 服务在「添加店铺」时经服务间 HTTP 调用触发，由 websocket 侧基于 Playwright
完成拼多多登录并抓取真实店铺信息（mallId / mallName / mallLogo）与用户信息，
**无需用户手填 shop_id**（需求 4.1 / 4.2 / 4.3 / 4.4）。

- ``POST /login/password``：账号密码登录，成功返回店铺与账号信息（含 Cookie，供
  backend 加密入库）；失败 / 超时（含人工验证未完成）返回失败响应。
- ``POST /login/cookie``：校验手动粘贴的 Cookie 文本并抓取店铺信息，成功返回同上
  结构；Cookie 无效返回失败响应。

接口约定（开发规范 1-3）：HTTP 恒返回 200，业务成败由统一响应体
``{code, success, message, data}`` 表达。地址经环境变量配置（禁止写死 localhost，
规范 21）。本路由不抛异常，失败统一规整为失败响应（健壮性兜底，需求 26）。

安全说明：返回体包含 Cookie 明文，仅用于 **服务间内网调用**（backend 收到后立即
可逆加密入库，不外泄给前端，需求 3.6）。

实现约束（开发规范）：导入置顶（规范 51）、中文注释（规范 37）、文件名用下划线
（规范 40）、单文件 ≤500 行（规范 35）、日志禁用 debug（规范 38）、复用共通（规范 36/52）。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter
from pydantic import BaseModel, Field

from channel_pdd import pdd_login
from common.core.platform_guard import PlatformDenied, enforce
from common.schemas.common import ApiResponse, error_response, success_response
from login.visible_login import run_visible_login

logger = logging.getLogger("websocket.routes.login")

# 店铺登录路由：标签便于 OpenAPI 分组；前缀由聚合层添加。
router = APIRouter(tags=["店铺登录"])


class PasswordLoginRequest(BaseModel):
    """账号密码登录请求体（与 backend shop_login_client 约定一致）。"""

    username: str = Field(..., description="拼多多商家后台登录账号")
    password: str = Field(..., description="拼多多商家后台登录密码（明文，仅内网传输）")


class CookieImportRequest(BaseModel):
    """Cookie 导入请求体（与 backend shop_login_client 约定一致）。"""

    cookies: str = Field(..., description="用户粘贴的 Cookie 文本")


@router.post(
    "/login/password",
    response_model=ApiResponse,
    summary="账号密码登录并返回店铺信息",
)
async def login_by_password(payload: PasswordLoginRequest) -> ApiResponse:
    """账号密码登录拼多多并抓取真实店铺信息（需求 4.1 / 4.2）。

    经 Playwright 完成账号密码登录（含人工验证等待），成功后基于 Cookie 抓取
    用户与店铺信息（mallId / mallName / mallLogo）。无论成败本路由都不抛异常：
    成功返回含 shop_id / shop_name / shop_logo / user_id / username / password /
    cookies 的数据；登录失败 / 超时返回失败响应。

    Args:
        payload: 含 username / password 的登录请求体。

    Returns:
        统一响应体：成功返回店铺与账号信息；失败返回中文原因。
    """
    try:
        enforce("login")
        info = await pdd_login.login_pdd(payload.username, payload.password)
    except PlatformDenied:
        return error_response(
            -1,
            "账号密码登录已停用。请使用官方页面登录核对；该步骤不保存密码或 Cookie。",
        )
    except Exception as exc:  # noqa: BLE001 - 登录异常不抛出，规整为失败响应
        logger.error("账号 '%s' 登录异常：%s", payload.username, exc)
        return error_response(-1, "账号密码登录失败，请稍后重试")

    if info is None:
        logger.warning("账号 '%s' 登录失败或未获取到店铺信息", payload.username)
        return error_response(-1, "账号密码登录失败，请检查账号密码或完成人工验证")

    return success_response(data=info, message="登录成功")


@router.post(
    "/login/cookie",
    response_model=ApiResponse,
    summary="校验 Cookie 文本并返回店铺信息",
)
async def login_by_cookie(payload: CookieImportRequest) -> ApiResponse:
    """校验手动粘贴的 Cookie 文本并抓取店铺信息（需求 4.3 / 4.4）。

    先做格式校验，再基于 Cookie 抓取店铺 / 用户信息验证其有效性。无论成败本路由
    都不抛异常：成功返回含 shop_id / shop_name / shop_logo / user_id / cookies 的
    数据；Cookie 无效 / 不完整返回失败响应。

    Args:
        payload: 含 cookies 文本的导入请求体。

    Returns:
        统一响应体：成功返回店铺与账号信息；失败返回中文原因。
    """
    try:
        enforce("login")
        result = pdd_login.import_by_cookie(payload.cookies)
    except PlatformDenied:
        return error_response(
            -1,
            "粘贴 Cookie 已停用。请使用官方页面登录核对；该步骤不导出 Cookie。",
        )
    except Exception as exc:  # noqa: BLE001 - 导入异常不抛出，规整为失败响应
        logger.error("Cookie 导入校验异常：%s", exc)
        return error_response(-1, "Cookie 导入失败，请稍后重试")

    if not result.get("success"):
        message = result.get("message") or "Cookie 无效或无法获取店铺信息"
        logger.warning("Cookie 导入校验失败：%s", message)
        return error_response(-1, message)

    return success_response(data=result.get("data"), message="Cookie 校验通过")


class VisibleLoginRequest(BaseModel):
    """管理端明确确认后，才打开官网登录页。"""

    confirm: str = Field(..., description="必须为 OPEN_OFFICIAL_LOGIN")


@router.post("/login/visible", response_model=ApiResponse, summary="打开官网登录页并只核对可见页面")
async def visible_page_login(payload: VisibleLoginRequest) -> ApiResponse:
    """人工打开官网登录页。不接收密码或 Cookie，也不创建收消息授权。"""
    from login.visible_browser import PlaywrightVisibleBrowser

    result = await run_visible_login(
        PlaywrightVisibleBrowser(), confirm=payload.confirm
    )
    if result.status != "visible_page_reached":
        return error_response(-1, result.message)
    return success_response(data=result.as_dict(), message=result.message)


@router.post("/login/visible/revoke", response_model=ApiResponse, summary="撤销官方页面登录许可")
async def revoke_visible_login() -> ApiResponse:
    """删除短时登录许可。等待中的页面核对会失败。"""
    from common.core.login_session import revoke

    revoke()
    return success_response(message="已撤销官方页面登录许可")


__all__ = ["router"]
