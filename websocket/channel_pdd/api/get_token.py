# -*- coding: utf-8 -*-
"""
channel_pdd.api.get_token —— 拼多多 Token 获取接口
==================================================
本文件用途：复用改造参照项目 Customer-Agent-1.2.0
``Channel/pinduoduo/utils/API/get_token.py``（class GetToken），基于本系统拼多多
基础请求层 ``BaseRequest``，在凭据有效时基于 Cookie 获取建立 WebSocket 连接所需的
Token（需求 4.8）。

两种使用方式：
1. 按店铺定位：``GetToken(shop_id=..., user_id=...)``，由 BaseRequest 自数据库加载
   并解密该店铺 Cookie 后请求（与连接服务建连前的调用口径一致）。
2. 直接注入 Cookie：``GetToken(cookies=...)``，登录刚获取 Cookie 尚未落库时使用。

差异说明（按本系统架构改造）：
- 继承本系统 ``channel_pdd.core.base_request.BaseRequest``；
- 修正参照项目中对不存在属性 ``self.account_name`` 的引用，改用 shop_id 记录日志；
- 日志使用本模块标准库 logger。

实现约束（开发规范）：导入置顶、中文注释、文件名用下划线。
"""
from __future__ import annotations

import logging
from typing import Any, Optional, Union

from channel_pdd.core.base_request import BaseRequest

logger = logging.getLogger("channel_pdd.get_token")

# Token 获取接口地址（拼多多商家后台聊天服务）。
TOKEN_URL: str = "https://mms.pinduoduo.com/chats/getToken"

# 接口版本参数（参照 Customer-Agent 实测固定为 "3"）。
TOKEN_API_VERSION: str = "3"


class GetToken(BaseRequest):
    """拼多多 WebSocket 连接 Token 获取。

    凭据有效时调用商家后台 ``/chats/getToken`` 接口获取 access_token，
    供建立 WebSocket 连接时注入握手参数（需求 4.8）。
    """

    def __init__(
        self,
        shop_id: Optional[str] = None,
        user_id: Optional[int] = None,
        channel_name: str = "pinduoduo",
        cookies: Optional[Union[dict, str]] = None,
    ) -> None:
        """构造 Token 获取实例。

        Args:
            shop_id: 拼多多店铺业务标识（与 user_id 同时提供时自数据库加载 Cookie）。
            user_id: 归属用户 ID。
            channel_name: 渠道名称（默认 pinduoduo）。
            cookies: 直接注入的 Cookie（字典或 JSON 字符串）；提供时覆盖数据库加载结果，
                用于登录刚获取 Cookie 尚未落库的场景。
        """
        super().__init__(shop_id=shop_id, user_id=user_id, channel_name=channel_name)
        if cookies:
            self.update_cookies(cookies)

    def get_token(self) -> Optional[str]:
        """获取建立 WebSocket 连接所需的 Token（需求 4.8）。

        Returns:
            成功返回 token 字符串；获取失败返回 None。
        """
        payload = {"version": TOKEN_API_VERSION}
        result: Optional[dict[str, Any]] = self.post(
            TOKEN_URL,
            data=payload,
            purpose="token",
            max_retries=0,
            allow_relogin=False,
        )

        if result:
            # 兼容两种返回结构：顶层 token 或 result.token。
            if "token" in result:
                return result["token"]
            nested = result.get("result")
            if isinstance(nested, dict) and "token" in nested:
                return nested["token"]
            logger.error("店铺 shop_id=%s 无法从响应中获取 token: %s", self.shop_id, result)

        return None


__all__ = ["GetToken", "TOKEN_URL", "TOKEN_API_VERSION"]
