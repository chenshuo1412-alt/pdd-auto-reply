# -*- coding: utf-8 -*-
"""
channel_pdd.api.set_cs_status —— 拼多多客服在线状态设置接口
==========================================================
本文件用途：复用改造参照项目 Customer-Agent-1.2.0
``Channel/pinduoduo/utils/API/Set_up_online.py``（class AccountMonitor.set_csstatus），
基于本系统拼多多基础请求层 ``BaseRequest`` 设置客服在线状态（需求 5：连接建立后
需将客服置为「在线」，拼多多才会向该客服推送客户消息）。

差异说明（按本系统架构改造）：
- 继承本系统 ``channel_pdd.core.base_request.BaseRequest``；按 shop_id / user_id
  从数据库加载并解密 Cookie（与连接 / 发送消息口径一致）。
- 日志统一为模块 logger（参照项目使用 self.logger）。

实现约束（开发规范）：导入置顶、中文注释、文件名用下划线（规范 40/51/37）。
"""
from __future__ import annotations

import logging
from typing import Optional

from channel_pdd.core.base_request import BaseRequest
from common.core.platform_guard import PlatformDenied

logger = logging.getLogger("channel_pdd.set_cs_status")

# 客服在线状态设置接口地址（拼多多商家后台聊天平台）。
CS_STATUS_URL: str = "https://mms.pinduoduo.com/plateau/chat/set_csstatus"

# 客服在线状态取值（与拼多多接口约定，参照 Customer-Agent set_csstatus）：
# 1=在线，3=离线。注意是整数，不是字符串（字符串会被接口判为非法状态）。
CS_STATUS_ONLINE: int = 1
CS_STATUS_OFFLINE: int = 3


class CsStatusSetter(BaseRequest):
    """拼多多客服在线状态设置。

    按 (shop_id, user_id) 定位账号并加载其 Cookie，调用商家后台接口将客服置为
    在线 / 离线。成功返回 True；失败返回 False（不抛异常）。
    """

    def __init__(
        self,
        shop_id: Optional[str] = None,
        user_id: Optional[int] = None,
        channel_name: str = "pinduoduo",
    ) -> None:
        """构造客服状态设置实例。

        Args:
            shop_id: 拼多多店铺业务标识（用于从库加载 Cookie）。
            user_id: 归属用户 ID。
            channel_name: 渠道名称（默认 pinduoduo）。
        """
        super().__init__(shop_id=shop_id, user_id=user_id, channel_name=channel_name)

    def set_status(self, status: int) -> bool:
        """设置客服在线状态。

        Args:
            status: 状态值（整数，1=在线 / 3=离线，参照 Customer-Agent 约定）。

        Returns:
            设置成功返回 True；失败返回 False。
        """
        data = {
            "data": {"cmd": "set_csstatus", "status": status},
            "client": "WEB",
        }
        result = self.post(
            CS_STATUS_URL,
            json_data=data,
            purpose="presence",
            max_retries=0,
            allow_relogin=False,
        )
        if result and result.get("success") is True:
            return True
        # 兼容拼多多多种错误字段命名（errorMsg / error_msg / errorMessage），
        # 取不到具体原因时回退打印完整响应，便于定位（result 为 None 表示请求失败）。
        error_msg = None
        if isinstance(result, dict):
            error_msg = (
                result.get("errorMsg")
                or result.get("error_msg")
                or result.get("errorMessage")
            )
        logger.error(
            "设置客服状态失败: status=%s, 原因=%s, 完整响应=%s",
            status,
            error_msg or "未知（请求失败或无错误字段）",
            result,
        )
        return False


def set_cs_online(
    shop_id: str, user_id: int, channel_name: str = "pinduoduo"
) -> bool:
    """将指定店铺账号的客服置为「在线」（连接建立后调用，需求 5）。

    Args:
        shop_id: 拼多多店铺业务标识。
        user_id: 归属用户 ID。
        channel_name: 渠道名称（默认 pinduoduo）。

    Returns:
        设置成功返回 True；失败返回 False（不抛异常，不影响连接主流程）。
    """
    try:
        return CsStatusSetter(
            shop_id=shop_id, user_id=user_id, channel_name=channel_name
        ).set_status(CS_STATUS_ONLINE)
    except PlatformDenied as exc:
        logger.info("未置在线: shop_id=%s, %s", shop_id, exc)
        return False
    except Exception as exc:  # noqa: BLE001 - 状态设置失败不影响连接主流程
        logger.error("设置客服在线异常: shop_id=%s, %s", shop_id, exc)
        return False


__all__ = [
    "CsStatusSetter",
    "CS_STATUS_URL",
    "CS_STATUS_ONLINE",
    "CS_STATUS_OFFLINE",
    "set_cs_online",
]
