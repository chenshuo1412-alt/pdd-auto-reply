# -*- coding: utf-8 -*-
"""
channel_pdd.core.pdd_token —— 拼多多 WebSocket 连接 Token 获取
=============================================================
本文件用途：复用 task 10.1 的拼多多基础请求层（``BaseRequest``）实现「建立
WebSocket 连接所需 access_token」的获取（需求 4.8 / 5.1）。参照 Customer-Agent
``Channel/pinduoduo/utils/API/get_token.py`` 的 ``GetToken`` 改造。

设计要点：
- ``GetToken`` 继承 ``BaseRequest``，复用统一请求 / 重试 / 会话过期自动重登能力；
  请求 ``https://mms.pinduoduo.com/chats/getToken`` 获取连接令牌。
- Token 获取与连接服务解耦：``PDDChannel`` 通过可注入的「token_provider」回调
  取得 access_token，默认采用本模块 ``default_token_provider``。当 task 10.2
  登录 / Token 模块就绪时，可注入其实现替换默认逻辑（缺位时安全降级）。

实现约束（开发规范）：单文件 ≤500 行、文件名用下划线、导入置顶、注释完善、全中文。
"""
from __future__ import annotations

import logging
from typing import Optional

from channel_pdd.core.base_request import BaseRequest

logger = logging.getLogger("channel_pdd.token")

# 拼多多连接 Token 获取接口（参照 Customer-Agent 实测）。
GET_TOKEN_URL: str = "https://mms.pinduoduo.com/chats/getToken"


class GetToken(BaseRequest):
    """拼多多连接 Token 获取（复用 BaseRequest 的请求 / 重试 / 重登能力）。"""

    def __init__(
        self, shop_id: str, user_id: int, channel_name: str = "pinduoduo"
    ) -> None:
        """初始化 Token 获取器。

        Args:
            shop_id: 拼多多店铺业务标识。
            user_id: 归属用户 ID。
            channel_name: 渠道名称（默认 pinduoduo）。
        """
        super().__init__(shop_id=shop_id, user_id=user_id, channel_name=channel_name)

    def get_token(self) -> Optional[str]:
        """获取建立 WebSocket 连接所需的 access_token（需求 4.8）。

        Returns:
            成功返回 token 字符串；失败返回 None（不抛异常，便于上层降级处理）。
        """
        result = self.post(
            GET_TOKEN_URL,
            data={"version": "3"},
            purpose="token",
            max_retries=0,
            allow_relogin=False,
        )
        if not result:
            logger.error("店铺 shop_id=%s 获取连接 Token 失败：空响应", self.shop_id)
            return None
        # 兼容两种响应结构：顶层 token 或 result.token。
        if isinstance(result.get("token"), str):
            return result["token"]
        nested = result.get("result")
        if isinstance(nested, dict) and isinstance(nested.get("token"), str):
            return nested["token"]
        logger.error("店铺 shop_id=%s 响应中未找到 token: %s", self.shop_id, result)
        return None


def default_token_provider(
    shop_id: str, user_id: int, channel_name: str = "pinduoduo"
) -> Optional[str]:
    """默认 Token 提供回调：经 BaseRequest 请求拼多多 getToken 接口。

    供 ``PDDChannel`` 在未注入自定义 token_provider 时使用；任何异常均降级为
    返回 None（不中断连接流程，由上层据此置「错误」并记录日志）。

    Args:
        shop_id: 店铺业务标识。
        user_id: 归属用户 ID。
        channel_name: 渠道名称。

    Returns:
        access_token 字符串；获取失败返回 None。
    """
    try:
        return GetToken(shop_id, user_id, channel_name).get_token()
    except Exception as exc:  # noqa: BLE001 - Token 获取异常安全降级
        logger.error("店铺 shop_id=%s 默认 Token 获取异常: %s", shop_id, exc)
        return None


__all__ = ["GetToken", "GET_TOKEN_URL", "default_token_provider"]
