# -*- coding: utf-8 -*-
"""
channel_pdd.transfer_service —— 转人工与商品卡片发送服务
========================================================
本文件用途：基于拼多多消息发送接口封装（``channel_pdd.api.send_message.SendMessage``）
实现「会话转移转人工」与「商品卡片发送」的业务编排，满足需求 16.1 / 16.2 / 16.3 /
16.4 / 16.5：

- 需求 16.1：``get_cs_list`` 查询某店铺可分配的人工客服列表（客服标识与名称）。
- 需求 16.2：``transfer_to_human`` 调用拼多多会话转移接口转人工，并记录消息日志
  为「已转人工」（process_result=``transferred``）。
- 需求 16.3：``evaluate_transfer`` 判定「客户消息触发转人工关键词」或「AI 判定需人工
  介入」，命中则应转人工并暂停对该会话的自动回复（返回的决策含 ``pause_auto_reply``）。
- 需求 16.4：``send_goods_card`` 调用拼多多商品卡片接口向客户会话发送商品卡片。
- 需求 16.5：会话转移或商品卡片发送失败（含因缺少有效 anti-content 签名导致商品卡片
  接口失败）时，记录消息日志为对应失败原因，且**不中断后续消息处理**（不抛异常）。

设计要点：
- 转人工关键词从 ``transfer_keyword`` 表（启用项）按店铺读取；判定逻辑（关键词匹配 /
  AI 需人工）为纯函数，便于单元测试。
- 数据访问经 common 仓储参数化执行（规范 16）；时间统一北京时间（规范 17）；
  日志禁用 debug（规范 38）。
- 网络 / 接口异常与签名缺失（``AntiContentMissingError``）均被捕获并落消息日志，
  对外返回结构化结果而非抛出，保证「失败记日志不中断」。

实现约束（开发规范）：单文件 ≤500 行、文件名用下划线、导入置顶、注释完善、全中文。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from channel_pdd.api.send_message import SendMessage
from channel_pdd.core.anti_content import AntiContentMissingError, SIGNATURE_MISSING_MESSAGE
from common.db.repository import Repository, run_in_session
from common.models.config_models import TransferKeyword
from common.models.log_models import MessageLog
from common.models.shop_models import Shop
from common.utils.time_utils import now_beijing_naive

logger = logging.getLogger("channel_pdd.transfer_service")

# 消息处理结果枚举键（与 common.services.dict_seed_data 的 process_result 一致）。
PROCESS_RESULT_TRANSFERRED: str = "transferred"  # 已转人工（需求 16.2）

# 转人工 / 卡片失败时记入消息日志的失败原因前缀（中文，需求 16.5）。
TRANSFER_FAILED_PREFIX: str = "转人工失败"
GOODS_CARD_FAILED_PREFIX: str = "商品卡片发送失败"


@dataclass
class TransferDecision:
    """转人工判定结果（纯数据结构，需求 16.3）。

    Attributes:
        should_transfer: 是否应转人工。
        pause_auto_reply: 是否应暂停对该会话的自动回复（转人工时为 True）。
        reason: 触发原因（中文，如「命中转人工关键词：人工」「AI 判定需人工介入」）。
        matched_keyword: 命中的转人工关键词（仅关键词触发时有值）。
    """

    should_transfer: bool = False
    pause_auto_reply: bool = False
    reason: str = ""
    matched_keyword: Optional[str] = None


@dataclass
class TransferResult:
    """转人工 / 商品卡片发送的操作结果（纯数据结构，需求 16.5）。

    Attributes:
        success: 操作是否成功。
        message: 结果说明（中文，失败时为失败原因）。
        downgrade: 是否因签名缺失需降级（仅商品卡片发送场景，需求 16.5 / 26.3）。
        data: 接口返回的原始数据（成功时）。
    """

    success: bool = False
    message: str = ""
    downgrade: bool = False
    delivery: str = "failed"
    data: Optional[Dict[str, Any]] = field(default=None)


# ----------------------------------------------------------------------
# 转人工触发判定（纯逻辑，需求 16.3）
# ----------------------------------------------------------------------
def evaluate_transfer(
    message_text: Optional[str],
    transfer_keywords: List[str],
    ai_needs_human: bool = False,
) -> TransferDecision:
    """判定是否应转人工并暂停自动回复（需求 16.3，纯函数便于测试）。

    判定规则（任一命中即应转人工）：
    1. 客户消息文本包含任一启用的转人工关键词；
    2. AI 回复引擎判定需人工介入（``ai_needs_human=True``）。

    Args:
        message_text: 客户消息文本（None / 空串视为无文本）。
        transfer_keywords: 该店铺启用的转人工关键词列表。
        ai_needs_human: AI 是否判定需人工介入。

    Returns:
        ``TransferDecision``：命中则 ``should_transfer`` 与 ``pause_auto_reply`` 均为 True。
    """
    text = (message_text or "").strip()
    if text:
        for keyword in transfer_keywords:
            kw = (keyword or "").strip()
            if kw and kw in text:
                return TransferDecision(
                    should_transfer=True,
                    pause_auto_reply=True,
                    reason=f"命中转人工关键词：{kw}",
                    matched_keyword=kw,
                )

    if ai_needs_human:
        return TransferDecision(
            should_transfer=True,
            pause_auto_reply=True,
            reason="AI 判定需人工介入",
        )

    return TransferDecision()


class TransferService:
    """转人工与商品卡片发送服务（需求 16.1-16.5）。

    封装客服列表查询、会话转移转人工、商品卡片发送，并统一处理失败落日志、
    签名缺失降级；转人工关键词判定经 ``load_transfer_keywords`` + ``evaluate_transfer``。
    """

    def __init__(
        self,
        shop_id: str,
        user_id: int,
        channel_name: str = "pinduoduo",
        sender: Optional[SendMessage] = None,
    ) -> None:
        """构造转人工服务实例。

        Args:
            shop_id: 拼多多店铺业务标识。
            user_id: 归属用户 ID。
            channel_name: 渠道名称（默认 pinduoduo）。
            sender: 可注入的 ``SendMessage`` 实例（便于测试注入桩）；缺省自建。
        """
        self.shop_id = shop_id
        self.user_id = user_id
        self.channel_name = channel_name
        self._sender = sender or SendMessage(
            shop_id=shop_id, user_id=user_id, channel_name=channel_name
        )

    # ------------------------------------------------------------------
    # 客服列表查询（需求 16.1）
    # ------------------------------------------------------------------
    def get_cs_list(self) -> Optional[List[Dict[str, Any]]]:
        """查询店铺可分配的人工客服列表（需求 16.1）。

        Returns:
            成功返回客服列表（客服标识与名称）；失败返回 None（失败记日志不抛异常）。
        """
        try:
            return self._sender.get_assign_cs_list()
        except Exception as exc:  # noqa: BLE001 - 查询失败不中断主流程
            logger.error("查询客服列表失败: shop_id=%s, %s", self.shop_id, exc)
            return None

    # ------------------------------------------------------------------
    # 转人工关键词加载与判定（需求 16.3）
    # ------------------------------------------------------------------
    def load_transfer_keywords(self) -> List[str]:
        """加载本店铺启用的转人工关键词（需求 16.3）。

        Returns:
            启用的转人工关键词列表；店铺不存在 / 无配置 / 数据库异常时返回空列表。
        """
        try:
            shop_pk = self._resolve_shop_pk()
            if shop_pk is None:
                return []

            def _handler(session: object) -> List[str]:
                rows = Repository(TransferKeyword, session).list(
                    filters={"shop_pk": shop_pk, "enabled": True}, order_by=False
                )
                return [row.keyword for row in rows if row.keyword]

            return run_in_session(_handler)
        except Exception as exc:  # noqa: BLE001 - 读取失败安全降级为无关键词
            logger.error("加载转人工关键词失败: shop_id=%s, %s", self.shop_id, exc)
            return []

    def evaluate(
        self, message_text: Optional[str], ai_needs_human: bool = False
    ) -> TransferDecision:
        """结合本店铺启用关键词与 AI 判定，给出转人工决策（需求 16.3）。

        Args:
            message_text: 客户消息文本。
            ai_needs_human: AI 是否判定需人工介入。

        Returns:
            ``TransferDecision`` 转人工决策（命中则暂停自动回复）。
        """
        keywords = self.load_transfer_keywords()
        return evaluate_transfer(message_text, keywords, ai_needs_human)

    # ------------------------------------------------------------------
    # 会话转移转人工（需求 16.2 / 16.5）
    # ------------------------------------------------------------------
    def transfer_to_human(
        self,
        recipient_uid: Any,
        cs_uid: Optional[Any] = None,
        message_content: Optional[str] = None,
    ) -> TransferResult:
        """将客户会话转接给指定人工客服并记消息日志（需求 16.2 / 16.5）。

        未指定 ``cs_uid`` 时自动取客服列表首位作为目标客服。成功记「已转人工」，
        失败记失败原因，均不抛异常（不中断后续消息处理）。

        Args:
            recipient_uid: 客户 UID。
            cs_uid: 目标人工客服标识；为空时自动选取客服列表首位。
            message_content: 触发转人工的原始客户消息（记入消息日志，便于追溯）。

        Returns:
            ``TransferResult`` 操作结果。
        """
        try:
            target_cs = cs_uid
            if target_cs is None:
                target_cs = self._pick_default_cs()
            if target_cs is None:
                reason = f"{TRANSFER_FAILED_PREFIX}：无可分配的人工客服"
                self._record_message_log(recipient_uid, message_content, reason)
                logger.error("%s: shop_id=%s", reason, self.shop_id)
                return TransferResult(success=False, message=reason)

            result = self._sender.move_conversation(recipient_uid, target_cs)
            delivery = getattr(getattr(self._sender, "last_delivery", None), "status", "failed")
            if delivery == "unknown":
                reason = f"{TRANSFER_FAILED_PREFIX}：发送结果不明，不重试"
                self._record_message_log(recipient_uid, message_content, reason)
                return TransferResult(success=False, message=reason, delivery="unknown")
            if result is not None:
                self._record_message_log(
                    recipient_uid, message_content, PROCESS_RESULT_TRANSFERRED
                )
                logger.info(
                    "会话已转人工: shop_id=%s, customer=%s, cs=%s",
                    self.shop_id, recipient_uid, target_cs,
                )
                return TransferResult(
                    success=True, message="已转人工", delivery="sent", data=result
                )

            reason = f"{TRANSFER_FAILED_PREFIX}：会话转移接口调用失败"
            self._record_message_log(recipient_uid, message_content, reason)
            logger.error("%s: shop_id=%s", reason, self.shop_id)
            return TransferResult(success=False, message=reason)
        except Exception as exc:  # noqa: BLE001 - 失败记日志不中断（需求 16.5）
            reason = f"{TRANSFER_FAILED_PREFIX}：{exc}"
            self._record_message_log(recipient_uid, message_content, reason)
            logger.error("转人工异常: shop_id=%s, %s", self.shop_id, exc)
            return TransferResult(success=False, message=reason)

    # ------------------------------------------------------------------
    # 商品卡片发送（需求 16.4 / 16.5 / 26.3）
    # ------------------------------------------------------------------
    def send_goods_card(
        self, recipient_uid: Any, goods_id: Any, biz_type: int = 2
    ) -> TransferResult:
        """向客户会话发送商品卡片（需求 16.4 / 16.5）。

        签名缺失 / 失效（``AntiContentMissingError``）时返回 ``downgrade=True`` 并记
        消息日志，供上层降级为文本回复商品信息（需求 16.5 / 26.3）；其它失败记失败
        原因。均不抛异常（不中断后续消息处理）。

        Args:
            recipient_uid: 客户 UID。
            goods_id: 商品 ID。
            biz_type: 业务类型，默认 2（客服推荐商品）。

        Returns:
            ``TransferResult`` 操作结果（签名缺失时 ``downgrade=True``）。
        """
        try:
            result = self._sender.send_mall_goods_card(
                recipient_uid, goods_id, biz_type=biz_type
            )
            if isinstance(result, dict):
                status = "sent" if result.get("success") else "failed"
            else:
                status = getattr(result, "status", "failed")
            if status == "sent":
                logger.info(
                    "商品卡片已发送: shop_id=%s, customer=%s, goods_id=%s",
                    self.shop_id, recipient_uid, goods_id,
                )
                return TransferResult(
                    success=True,
                    message="商品卡片已发送",
                    delivery="sent",
                    data=getattr(result, "payload", None),
                )
            if status == "unknown":
                reason = f"{GOODS_CARD_FAILED_PREFIX}：发送结果不明，不重试"
                self._record_message_log(recipient_uid, None, reason)
                return TransferResult(success=False, message=reason, delivery="unknown")

            reason = f"{GOODS_CARD_FAILED_PREFIX}：接口调用失败"
            self._record_message_log(recipient_uid, None, reason)
            logger.error("%s: shop_id=%s, goods_id=%s", reason, self.shop_id, goods_id)
            return TransferResult(success=False, message=reason)
        except AntiContentMissingError as exc:
            # 签名缺失 / 失效：标记降级供上层改以文本回复商品信息（需求 16.5 / 26.3）。
            reason = f"{GOODS_CARD_FAILED_PREFIX}：{exc.message or SIGNATURE_MISSING_MESSAGE}"
            self._record_message_log(recipient_uid, None, reason)
            logger.warning(
                "商品卡片因签名缺失降级为文本: shop_id=%s, goods_id=%s",
                self.shop_id, goods_id,
            )
            return TransferResult(success=False, message=reason, downgrade=True)
        except Exception as exc:  # noqa: BLE001 - 失败记日志不中断（需求 16.5）
            reason = f"{GOODS_CARD_FAILED_PREFIX}：{exc}"
            self._record_message_log(recipient_uid, None, reason)
            logger.error("商品卡片发送异常: shop_id=%s, %s", self.shop_id, exc)
            return TransferResult(success=False, message=reason)

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    def _pick_default_cs(self) -> Optional[Any]:
        """选取默认目标人工客服（客服列表首位）。

        说明（与参考项目 Customer-Agent-1.2.0 的差异）：参考项目用
        ``cs_{shop_id}_{user_id}`` 排除「本账号自己」，其 user_id 即拼多多账号 uid；
        而本系统 ``user_id`` 为平台归属用户 ID，且未持久化拼多多账号自身 uid，无法
        可靠重建自身 cs_uid，故此处不做「排除自己」的猜测式过滤（避免误判），
        统一取规整后客服列表首位作为目标客服。

        Returns:
            首位客服 cs_uid（兼容规整后的 cs_uid 及历史 csid/uid/id 命名）；
            列表为空 / 失败返回 None。
        """
        cs_list = self.get_cs_list()
        if not cs_list:
            return None
        first = cs_list[0]
        if isinstance(first, dict):
            for key in ("cs_uid", "csid", "uid", "id"):
                if first.get(key) is not None:
                    return first[key]
            return None
        return first

    def _resolve_shop_pk(self) -> Optional[int]:
        """按 (owner_user_id, shop_id) 定位店铺主键 shop_pk。

        Returns:
            店铺主键 id；店铺不存在 / 数据库异常时返回 None。
        """
        def _handler(session: object) -> Optional[int]:
            shop = Repository(Shop, session).get_by(
                owner_user_id=self.user_id, shop_id=self.shop_id
            )
            return shop.id if shop is not None else None

        try:
            return run_in_session(_handler)
        except Exception as exc:  # noqa: BLE001 - 定位失败安全降级
            logger.error("定位店铺主键失败: shop_id=%s, %s", self.shop_id, exc)
            return None

    def _record_message_log(
        self,
        customer_uid: Optional[Any],
        message_content: Optional[str],
        process_result: str,
    ) -> None:
        """写入一条消息处理日志（需求 16.2 / 16.5）。

        转人工成功记「已转人工」，失败记失败原因；写日志失败仅记本地日志，不向上
        抛出（保证「失败记日志不中断」）。

        Args:
            customer_uid: 客户 UID。
            message_content: 原始消息内容（可选）。
            process_result: 处理结果（枚举键或失败原因文案）。
        """
        try:
            shop_pk = self._resolve_shop_pk()
            if shop_pk is None:
                logger.warning(
                    "记录消息日志时未找到店铺: shop_id=%s, user_id=%s",
                    self.shop_id, self.user_id,
                )
                return

            def _handler(session: object) -> None:
                Repository(MessageLog, session).create(
                    shop_pk=shop_pk,
                    customer_uid=str(customer_uid) if customer_uid is not None else None,
                    message_content=message_content,
                    process_result=process_result,
                    log_time=now_beijing_naive(),
                    created_by=self.user_id,
                )

            run_in_session(_handler)
        except Exception as exc:  # noqa: BLE001 - 写日志失败不中断主流程
            logger.error("记录转人工 / 卡片消息日志失败: shop_id=%s, %s", self.shop_id, exc)


__all__ = [
    "TransferService",
    "TransferDecision",
    "TransferResult",
    "evaluate_transfer",
    "PROCESS_RESULT_TRANSFERRED",
    "TRANSFER_FAILED_PREFIX",
    "GOODS_CARD_FAILED_PREFIX",
]
