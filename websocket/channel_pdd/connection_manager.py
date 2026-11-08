# -*- coding: utf-8 -*-
"""
channel_pdd.connection_manager —— 连接 + 消费器装配（端到端串联收口）
====================================================================
本文件用途：把「收消息（PDDChannel） → 解析入队 → 消费器处理链（决策/AI/降级/
转人工/记日志/通知）」装配为一条可启动的端到端链路（任务 19.2），并在
``connection_registry`` 中登记，供 backend 断连接口与连接状态查询统一定位。

串联方式：
- 为每个店铺构造一个 ``PDDChannel``，并将其 ``message_handler`` 绑定到本店铺的
  ``MessageConsumer.consume_raw``：PDDChannel 收到原始报文写入 FIFO 队列后即触发
  消费器消费，完成解析 → 决策链 → 知识库/AI → 发送回复/卡片降级 → 记日志/通知。
- ``start_channel`` 启动连接并登记到 ``connection_registry``；``stop_channel`` 停止
  并注销；断连复用 ``connection_registry.disconnect``（需求 3.5）。

实现约束（开发规范）：导入置顶（51）、中文注释（37）、单文件 ≤500 行（35）、
文件名用下划线（40）、全中文（50）、复用既有组件（52）。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from channel_pdd import connection_registry
from channel_pdd.message_queue import message_queue_manager
from channel_pdd.pdd_channel import PDDChannel
from common.db.repository import Repository
from common.db.session import session_scope
from common.models.shop_models import Shop
from engine.message_consumer import MessageConsumer, build_notifier

logger = logging.getLogger("channel_pdd.connection_manager")

# 店铺启用状态值（与 common.models.shop_models.Shop.status 约定一致：1=启用）。
_SHOP_STATUS_ENABLED: int = 1


def build_message_handler(consumer: MessageConsumer):
    """构造 PDDChannel 的消息消费回调：把原始报文交给消费器处理。

    Args:
        consumer: 本店铺的消息处理消费器。

    Returns:
        异步回调 ``handler(raw_message, shop_id, user_id)``，异常被消费器内部
        吞掉，不影响 PDDChannel 的消息接收循环。
    """

    async def _handler(raw_message: Any, shop_id: str, user_id: int) -> None:
        # 消费器内部已对解析 / 处理异常做兜底，这里再加一层保护避免影响接收循环。
        try:
            await consumer.consume_raw(raw_message)
        except Exception as exc:  # noqa: BLE001 - 消费异常不中断后续消息接收
            logger.error("消费消息异常: shop_id=%s, %s", shop_id, exc)

    return _handler


def create_channel(
    shop_id: str,
    shop_pk: int,
    user_id: int,
    *,
    channel_name: str = "pinduoduo",
    enable_notify: bool = True,
    consumer: Optional[MessageConsumer] = None,
) -> PDDChannel:
    """创建一个「连接 + 消费器」已串联的 PDDChannel（不自动启动）。

    Args:
        shop_id: 拼多多店铺业务标识。
        shop_pk: 店铺主键 shop.id。
        user_id: 归属用户 ID。
        channel_name: 渠道名称（默认 pinduoduo）。
        enable_notify: 是否启用系统事件通知（经 backend HTTP 推送）。
        consumer: 可注入的消费器（便于测试）；缺省按本店铺构造。

    Returns:
        已绑定消费器与独立 FIFO 队列的 PDDChannel 实例。
    """
    if consumer is None:
        consumer = MessageConsumer(
            shop_id=shop_id,
            shop_pk=shop_pk,
            user_id=user_id,
            channel_name=channel_name,
            notifier=build_notifier(shop_pk) if enable_notify else None,
        )

    # 为本店铺分配独立 FIFO 队列（入队顺序 == 消费顺序，需求 5.3）。
    queue = message_queue_manager.get_or_create(f"{user_id}:{shop_id}")

    channel = PDDChannel(
        shop_id=shop_id,
        user_id=user_id,
        channel_name=channel_name,
        message_queue=queue,
        message_handler=build_message_handler(consumer),
    )
    return channel


async def start_channel(
    shop_id: str,
    shop_pk: int,
    user_id: int,
    *,
    channel_name: str = "pinduoduo",
    enable_notify: bool = True,
) -> bool:
    """创建、启动并登记一个店铺连接（端到端链路就绪，需求 5.1 / 5.3）。

    Args:
        shop_id: 拼多多店铺业务标识。
        shop_pk: 店铺主键 shop.id。
        user_id: 归属用户 ID。
        channel_name: 渠道名称（默认 pinduoduo）。
        enable_notify: 是否启用系统事件通知。

    Returns:
        实际启动并登记返回 True；安全模式拒绝时返回 False。
    """
    # 幂等保护：同店铺已有活跃连接则跳过，避免重复建连（参照项目 is_running 判断）。
    existing = connection_registry.get(shop_id, user_id)
    if existing is not None:
        logger.info(
            "店铺已有活跃连接，跳过重复启动: shop_id=%s, user_id=%s", shop_id, user_id
        )
        return True

    channel = create_channel(
        shop_id,
        shop_pk,
        user_id,
        channel_name=channel_name,
        enable_notify=enable_notify,
    )
    started = await channel.start()
    if not started:
        logger.warning("店铺连接未启动: shop_id=%s", shop_id)
        return False
    connection_registry.register(shop_id, user_id, channel)
    logger.info("店铺连接已启动并登记: shop_id=%s, user_id=%s", shop_id, user_id)
    return True


async def start_enabled_channels() -> int:
    """启动全部「已启用」店铺的连接（服务启动时自动拉起，参照项目「启动所有」）。

    从数据库读取所有 ``status=启用`` 的店铺，逐个调用 ``start_channel`` 启动其
    拼多多长连接并装配消息处理全链路；已有活跃连接的店铺由 ``start_channel``
    幂等跳过。单个店铺启动失败仅记日志、不中断其它店铺（健壮性兜底，需求 26）。

    Returns:
        本次实际新启动的店铺连接数量。
    """
    # 一次性读出启用店铺的最小必要字段（独立短事务，读完即释放连接）。
    shops: list[tuple[str, int, Optional[int]]] = []
    try:
        with session_scope() as session:
            enabled_shops = Repository(Shop, session).list(
                filters={"status": _SHOP_STATUS_ENABLED}, order_by=False
            )
            for shop in enabled_shops:
                shop_id = str(shop.shop_id or "").strip()
                if not shop_id:
                    continue
                shops.append((shop_id, shop.id, shop.owner_user_id))
    except Exception as exc:  # noqa: BLE001 - 读库失败不应中断服务启动
        logger.error("读取启用店铺列表失败，跳过自动启动连接: %s", exc)
        return 0

    if not shops:
        logger.info("无已启用店铺，跳过自动启动连接")
        return 0

    logger.info("服务启动：开始自动拉起 %d 个已启用店铺的连接", len(shops))
    started = 0
    for shop_id, shop_pk, owner_user_id in shops:
        try:
            if await start_channel(shop_id, shop_pk, owner_user_id):
                started += 1
        except Exception as exc:  # noqa: BLE001 - 单店铺启动失败不影响其它店铺
            logger.error("自动启动店铺连接失败: shop_id=%s, %s", shop_id, exc)

    logger.info("自动启动已启用店铺连接完成：成功 %d/%d", started, len(shops))
    return started


async def stop_channel(shop_id: str, user_id: Optional[int]) -> bool:
    """停止并注销指定店铺连接（复用注册表断连，幂等，需求 3.5）。

    停止后从全局队列管理器移除本店铺的 FIFO 队列，避免店铺频繁启停在
    ``message_queue_manager`` 中残留队列对象造成内存泄漏。

    Args:
        shop_id: 拼多多店铺业务标识。
        user_id: 归属用户 ID（可空）。

    Returns:
        断开成功（或本就无连接）返回 True；停止出错返回 False。
    """
    ok = await connection_registry.disconnect(shop_id, user_id)
    # 断连后移除本店铺队列（与 create_channel 的命名一致），释放内存。
    if user_id is not None:
        message_queue_manager.remove(f"{user_id}:{shop_id}")
    return ok


__all__ = [
    "build_message_handler",
    "create_channel",
    "start_channel",
    "start_enabled_channels",
    "stop_channel",
]
