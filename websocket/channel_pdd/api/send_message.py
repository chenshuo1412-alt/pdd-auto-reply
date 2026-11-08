# -*- coding: utf-8 -*-
"""
channel_pdd.api.send_message —— 拼多多消息发送 / 客服列表 / 会话转移接口
======================================================================
本文件用途：复用改造参照项目 Customer-Agent-1.2.0
``Channel/pinduoduo/utils/API/send_message.py``（class SendMessage），基于本系统
拼多多基础请求层 ``BaseRequest``，封装拼多多商家后台的以下接口，支撑「会话转移
转人工与商品卡片发送」（需求 16.1 / 16.2 / 16.4 / 16.5）：

- ``send_text(recipient_uid, content)``：发送文本消息（不依赖 anti-content 签名）。
- ``send_image(recipient_uid, image_url)``：发送图片消息（不依赖签名）。
- ``send_mall_goods_card(recipient_uid, goods_id, biz_type=2)``：发送商品卡片
  （**依赖 anti-content 签名**，需求 26.1；缺失 / 失效时由 BaseRequest 抛
  ``AntiContentMissingError`` 供上层降级，需求 16.5 / 26.3）。
- ``get_assign_cs_list()``：查询店铺可分配的人工客服列表（需求 16.1）。
- ``move_conversation(recipient_uid, cs_uid)``：会话转移转人工（需求 16.2）。

差异说明（按本系统架构改造）：
- 继承本系统 ``channel_pdd.core.base_request.BaseRequest``（统一请求 / 重试 / 会话
  过期自动重登 / 签名检测），不再依赖参照项目的 ``self.logger`` / ``self.account_name``，
  统一使用模块级标准库 logger（规范 38：禁用 debug）。
- 商品卡片接口将 anti-content 注入请求头，并以 ``check_signature=True`` 让
  BaseRequest 在请求前校验签名存在、请求后检测签名失效（需求 26.2）。

实现约束（开发规范）：单文件 ≤500 行、文件名用下划线、导入置顶、注释完善、全中文。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from channel_pdd.core.base_request import BaseRequest, TransportUnclear
from common.core.platform_guard import PlatformDenied

logger = logging.getLogger("channel_pdd.send_message")

# 发送文本 / 图片消息接口地址（不依赖 anti-content 签名）。
SEND_MESSAGE_URL: str = "https://mms.pinduoduo.com/plateau/chat/send_message"

# 发送商城商品卡片接口地址（依赖 anti-content 签名，需求 26.1）。
MALL_GOODS_CARD_URL: str = "https://mms.pinduoduo.com/plateau/message/send/mallGoodsCard"

# 查询可分配客服列表接口地址（需求 16.1）。
ASSIGN_CS_LIST_URL: str = "https://mms.pinduoduo.com/latitude/assign/getAssignCsList"

# 会话转移转人工接口地址（需求 16.2）。
MOVE_CONVERSATION_URL: str = "https://mms.pinduoduo.com/plateau/chat/move_conversation"

# 拼多多「发送文本消息」失败的业务错误码（参照实测）。
_SEND_TEXT_ERROR_CODE: int = 10002


@dataclass
class SendResult:
    """一次发送的确定结果。unknown 表示可能已送达，调用方禁止自动重发。"""

    status: str
    payload: Optional[Dict[str, Any]] = None
    detail: str = ""


class SendMessage(BaseRequest):
    """拼多多消息发送、客服列表查询与会话转移接口封装。

    依赖签名的能力（商品卡片）与不依赖签名的能力（文本 / 图片 / 客服列表 /
    会话转移）共用同一基类；签名缺失仅影响商品卡片，转人工等核心能力照常可用
    （需求 26.4）。
    """

    def __init__(
        self,
        shop_id: Optional[str] = None,
        user_id: Optional[int] = None,
        channel_name: str = "pinduoduo",
    ) -> None:
        """构造消息发送实例。

        Args:
            shop_id: 拼多多店铺业务标识（与 user_id 同时提供时自数据库加载 Cookie）。
            user_id: 归属用户 ID。
            channel_name: 渠道名称（默认 pinduoduo）。
        """
        super().__init__(shop_id=shop_id, user_id=user_id, channel_name=channel_name)
        self.last_delivery = SendResult("failed", detail="尚未发送")

    def _deliver(
        self,
        url: str,
        json_data: Dict[str, Any],
        customer_uid: Any,
        headers: Optional[Dict[str, str]] = None,
        check_signature: bool = False,
    ) -> SendResult:
        """单次发送。超时、断连和 5xx 记为 unknown，不做 HTTP 重试，也不自动重登。"""
        try:
            result = self.post(
                url,
                json_data=json_data,
                headers=headers,
                check_signature=check_signature,
                purpose="send",
                customer_uid=None if customer_uid is None else str(customer_uid),
                max_retries=0,
                allow_relogin=False,
                transport_raises=True,
                timeout=float(os.environ.get("PDD_SEND_TIMEOUT", "15")),
            )
        except PlatformDenied as exc:
            self.last_delivery = SendResult("failed", detail=str(exc))
            return self.last_delivery
        except TransportUnclear as exc:
            self.last_delivery = SendResult("unknown", detail=str(exc))
            return self.last_delivery
        if not isinstance(result, dict):
            self.last_delivery = SendResult("unknown", detail="空响应")
            return self.last_delivery
        if result.get("success") is True:
            inner = result.get("result") or {}
            if isinstance(inner, dict) and inner.get("error_code") == _SEND_TEXT_ERROR_CODE:
                self.last_delivery = SendResult(
                    "failed", payload=result, detail=str(inner.get("error") or "业务拒绝")
                )
                return self.last_delivery
            self.last_delivery = SendResult("sent", payload=result)
            return self.last_delivery
        self.last_delivery = SendResult("failed", payload=result, detail="对端明确拒绝")
        return self.last_delivery

    # ------------------------------------------------------------------
    # 文本 / 图片消息（不依赖 anti-content 签名，需求 26.4）
    # ------------------------------------------------------------------
    def send_text(
        self, recipient_uid: Any, message_content: str
    ) -> Optional[Dict[str, Any]]:
        """发送文本消息。

        Args:
            recipient_uid: 接收消息的客户 UID。
            message_content: 文本内容。

        Returns:
            SendResult。sent 才表示对端明确接受。
        """
        data = {
            "data": {
                "cmd": "send_message",
                "request_id": self.generate_request_id(),
                "message": {
                    "to": {"role": "user", "uid": recipient_uid},
                    "from": {"role": "mall_cs"},
                    "content": message_content,
                    "msg_id": None,
                    "type": 0,
                    "is_aut": 0,
                    "manual_reply": 1,
                },
            },
            "client": "WEB",
        }

        delivered = self._deliver(SEND_MESSAGE_URL, data, recipient_uid)
        if delivered.status != "sent":
            logger.error(
                "发送文本消息未成功: shop_id=%s, status=%s, detail=%s",
                self.shop_id, delivered.status, delivered.detail,
            )
        return delivered

    def send_image(
        self, recipient_uid: Any, image_url: str
    ) -> Optional[Dict[str, Any]]:
        """发送图片消息。

        Args:
            recipient_uid: 接收消息的客户 UID。
            image_url: 图片 URL。

        Returns:
            成功返回响应字典；失败返回 None。
        """
        data = {
            "data": {
                "cmd": "send_message",
                "request_id": self.generate_request_id(),
                "message": {
                    "to": {"role": "user", "uid": recipient_uid},
                    "from": {"role": "mall_cs"},
                    "content": image_url,
                    "msg_id": None,
                    "chat_type": "cs",
                    "type": 1,
                    "is_aut": 0,
                    "manual_reply": 1,
                },
            },
            "client": "WEB",
        }

        return self._deliver(SEND_MESSAGE_URL, data, recipient_uid)

    # ------------------------------------------------------------------
    # 商品卡片（依赖 anti-content 签名，需求 16.4 / 26.1）
    # ------------------------------------------------------------------
    def send_mall_goods_card(
        self, recipient_uid: Any, goods_id: Any, biz_type: int = 2
    ) -> Optional[Dict[str, Any]]:
        """发送商城商品卡片消息（依赖 anti-content 签名）。

        以 ``check_signature=True`` 让 BaseRequest 在请求前校验签名存在、请求后
        检测签名失效；签名缺失 / 失效将抛出 ``AntiContentMissingError`` 供上层
        降级为文本回复（需求 16.5 / 26.3）。

        Args:
            recipient_uid: 接收消息的客户 UID。
            goods_id: 商品 ID。
            biz_type: 业务类型，默认 2（客服推荐商品）。

        Returns:
            SendResult。签名缺失仍抛 AntiContentMissingError，供上层降级。

        Raises:
            AntiContentMissingError: 当前 Cookie 缺少有效签名或接口返回签名失效。
        """
        data = {"uid": recipient_uid, "goods_id": goods_id, "biz_type": biz_type}

        # anti-content 从 Cookie 中取（兼容下划线 / 连字符两种命名），注入请求头。
        anti_content = self.cookies.get("anti_content") or self.cookies.get(
            "anti-content", ""
        )
        headers = {
            "accept": "application/json, text/plain, */*",
            "anti-content": anti_content,
            "content-type": "application/json;charset=UTF-8",
            "origin": "https://mms.pinduoduo.com",
            "referer": "https://mms.pinduoduo.com/chat-merchant/index.html",
        }

        delivered = self._deliver(
            MALL_GOODS_CARD_URL,
            data,
            recipient_uid,
            headers=headers,
            check_signature=True,
        )
        result = delivered.payload
        if delivered.status == "sent" and result and result.get("success") is True:
            logger.info(
                "商品卡片发送成功: shop_id=%s, goods_id=%s, to=%s",
                self.shop_id, goods_id, recipient_uid,
            )
            return delivered
        logger.error(
            "商品卡片发送失败: shop_id=%s, status=%s, detail=%s",
            self.shop_id, delivered.status, delivered.detail,
        )
        return delivered

    # ------------------------------------------------------------------
    # 客服列表查询（需求 16.1）
    # ------------------------------------------------------------------
    def get_assign_cs_list(self) -> Optional[List[Dict[str, Any]]]:
        """查询店铺可分配的人工客服列表（需求 16.1）。

        拼多多 ``getAssignCsList`` 的 ``result.csList`` 实际为「以客服 cs_uid 为键」的
        字典（参照 Customer-Agent-1.2.0），并非数组；为兼容历史与不同返回形态，这里
        统一规整为列表，每项含 ``cs_uid``（客服标识）与 ``cs_name``（客服名称）。

        Returns:
            成功返回规整后的客服列表（每项含 cs_uid / cs_name）；失败返回 None。
        """
        data = {"wechatCheck": True}
        result = self.post(ASSIGN_CS_LIST_URL, json_data=data, purpose="read", max_retries=0)
        if result and result.get("success") is True:
            inner = result.get("result", {}) or {}
            cs_raw = inner.get("csList")
            normalized = self._normalize_cs_list(cs_raw)
            if normalized is not None:
                return normalized
            logger.error("客服列表响应缺少 csList: shop_id=%s", self.shop_id)
            return None
        error_msg = (result or {}).get("result", {}).get("error") if result else "请求失败"
        logger.error("获取分配的客服列表失败: shop_id=%s, %s", self.shop_id, error_msg)
        return None

    @staticmethod
    def _normalize_cs_list(cs_raw: Any) -> Optional[List[Dict[str, Any]]]:
        """将拼多多返回的 csList 规整为统一的客服列表（每项含 cs_uid / cs_name）。

        兼容两种返回形态：
        - 字典：以 cs_uid 为键、客服信息为值（拼多多实际返回形态，参照 Customer-Agent）；
        - 列表：每项为含 csid/cs_uid/uid 等键的客服信息字典。

        Args:
            cs_raw: 原始 csList（dict / list / None / 其它）。

        Returns:
            规整后的客服列表；无法识别（None 或非 dict/list）时返回 None。
        """
        def _pick_name(info: Any, fallback: str) -> str:
            """从客服信息中提取名称，缺失时回退为 cs_uid。"""
            if isinstance(info, dict):
                # username 为拼多多实际返回的客服名称字段（参照 Customer-Agent-1.2.0）。
                for key in ("cs_name", "csName", "username", "name", "nickname", "nickName"):
                    value = info.get(key)
                    if value:
                        return str(value)
            return fallback

        # 字典形态：键为 cs_uid，值为客服信息。
        if isinstance(cs_raw, dict):
            result: List[Dict[str, Any]] = []
            for uid, info in cs_raw.items():
                cs_uid = str(uid)
                result.append({"cs_uid": cs_uid, "cs_name": _pick_name(info, cs_uid)})
            return result

        # 列表形态：每项为客服信息字典，提取 cs_uid 与名称。
        if isinstance(cs_raw, list):
            result = []
            for item in cs_raw:
                if isinstance(item, dict):
                    cs_uid = None
                    for key in ("cs_uid", "csid", "uid", "id"):
                        if item.get(key) is not None:
                            cs_uid = str(item[key])
                            break
                    if cs_uid is None:
                        continue
                    result.append({"cs_uid": cs_uid, "cs_name": _pick_name(item, cs_uid)})
                elif item is not None:
                    cs_uid = str(item)
                    result.append({"cs_uid": cs_uid, "cs_name": cs_uid})
            return result

        return None

    # ------------------------------------------------------------------
    # 会话转移转人工（需求 16.2）
    # ------------------------------------------------------------------
    def move_conversation(
        self, recipient_uid: Any, cs_uid: Any, remark: str = "无原因直接转移"
    ) -> Optional[Dict[str, Any]]:
        """将客户会话转移给指定人工客服（需求 16.2）。

        Args:
            recipient_uid: 客户 UID。
            cs_uid: 目标人工客服标识。
            remark: 转移备注（默认「无原因直接转移」）。

        Returns:
            成功返回响应字典；失败返回 None。
        """
        data = {
            "data": {
                "cmd": "move_conversation",
                "request_id": self.generate_request_id(),
                "conversation": {
                    "csid": cs_uid,
                    "uid": recipient_uid,
                    "need_wx": False,
                    "remark": remark,
                },
            },
            "client": "WEB",
        }

        delivered = self._deliver(MOVE_CONVERSATION_URL, data, recipient_uid)
        result = delivered.payload if delivered.status == "sent" else None
        if delivered.status == "sent" and result and result.get("success") is True:
            logger.info(
                "会话转移成功: shop_id=%s, to_cs=%s, customer=%s",
                self.shop_id, cs_uid, recipient_uid,
            )
            return result
        logger.error("会话转移失败: shop_id=%s, 响应=%s", self.shop_id, result)
        return None


__all__ = [
    "SendMessage",
    "SendResult",
    "SEND_MESSAGE_URL",
    "MALL_GOODS_CARD_URL",
    "ASSIGN_CS_LIST_URL",
    "MOVE_CONVERSATION_URL",
]
