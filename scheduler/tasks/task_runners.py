# -*- coding: utf-8 -*-
"""
scheduler.tasks.task_runners —— 各定时任务执行体
================================================
本文件用途：为 scheduler 服务定义三类定时任务的「执行体」，被 SchedulerService
按 ``scheduled_task`` 配置注册到调度器并周期触发（需求 21.2 / 21.4）：

- ``run_cookie_refresh``：遍历启用店铺，经 HTTP 调用 websocket 服务刷新 Cookie；
- ``run_product_sync``：遍历启用店铺，经 HTTP 调用 backend 服务触发商品同步；
- ``run_log_file_cleanup``：按保留天数仅清理磁盘日志文件（数据库业务日志表禁止
  物理删除）。

每个执行体均：
1. 完成各自业务；
2. 经 ``task_run_log`` 写一条 success / failed 执行日志（需求 21.2）；
3. 自身不向上抛异常（捕获后记失败日志），避免单次任务异常影响调度器存活。

实现约束（开发规范）：导入置顶（规范 51）、中文注释（规范 37）、文件名用下划线
（规范 40）、单文件 ≤500 行（规范 35）、参数化查询（规范 16）、连接失败重试
（规范 13）。
"""
from __future__ import annotations

import logging
from typing import List, Tuple

from sqlalchemy.orm import Session

from common.core.platform_guard import scheduler_platform_enabled
from common.db.repository import Repository, run_with_retry
from common.models.shop_models import Shop

from tasks import log_cleanup, service_client, task_run_log
from tasks.constants import (
    TASK_COOKIE_REFRESH,
    TASK_LOG_FILE_CLEANUP,
    TASK_PRODUCT_SYNC,
)

# 模块级日志记录器（禁用 debug 级别 —— 规范 38）。
logger = logging.getLogger("scheduler.task_runners")

# 店铺「启用」状态值（与 shop_models.Shop.status 约定一致：1=启用）。
_SHOP_STATUS_ENABLED: int = 1


def _list_enabled_shops(session: Session) -> List[Tuple[int, str, int | None]]:
    """查询全部启用状态的店铺（参数化查询）。

    Args:
        session: 事务性会话（由 run_with_retry 管理）。

    Returns:
        列表，每项为 (shop_pk, shop_id, owner_user_id) 三元组。
    """
    shops = Repository(Shop, session).list(filters={"status": _SHOP_STATUS_ENABLED})
    return [(shop.id, shop.shop_id, shop.owner_user_id) for shop in shops]


def run_cookie_refresh() -> None:
    """Cookie 刷新任务执行体（需求 4.6 / 21.2）。

    遍历全部启用店铺，逐个经 HTTP 调用 websocket 服务刷新 Cookie；汇总成功 /
    失败数量并写一条执行日志。任一店铺调用失败不中断整体遍历。
    安全模式下一律不触发。
    """
    if not scheduler_platform_enabled():
        task_run_log.write_success(TASK_COOKIE_REFRESH, "安全模式，跳过 Cookie 刷新，未访问平台")
        return
    try:
        shops = run_with_retry(_list_enabled_shops)
    except Exception as exc:  # noqa: BLE001 —— 取店铺失败：记失败日志后返回
        logger.error("Cookie 刷新任务取店铺列表失败：%s", exc)
        task_run_log.write_failed(TASK_COOKIE_REFRESH, f"取店铺列表失败：{exc}")
        return

    if not shops:
        task_run_log.write_success(TASK_COOKIE_REFRESH, "无启用店铺，跳过 Cookie 刷新")
        return

    success_count = 0
    failed_count = 0
    for shop_pk, shop_id, owner_user_id in shops:
        result = service_client.trigger_cookie_refresh(shop_pk, shop_id, owner_user_id)
        if result.ok:
            success_count += 1
        else:
            failed_count += 1
            logger.warning("店铺[%s] Cookie 刷新失败：%s", shop_id, result.message)

    message = f"Cookie 刷新完成：成功 {success_count} 个，失败 {failed_count} 个"
    # 只要有失败店铺即记为 failed，便于运维感知（但任务本身已尽力执行全部店铺）。
    if failed_count > 0:
        task_run_log.write_failed(TASK_COOKIE_REFRESH, message)
    else:
        task_run_log.write_success(TASK_COOKIE_REFRESH, message)


def run_product_sync() -> None:
    """商品同步任务执行体（需求 15.2 / 21.2）。

    遍历全部启用店铺，逐个经 HTTP 调用 backend 服务触发商品同步；汇总成功 /
    失败数量并写一条执行日志。任一店铺调用失败不中断整体遍历。
    安全模式下一律不触发。
    """
    if not scheduler_platform_enabled():
        task_run_log.write_success(TASK_PRODUCT_SYNC, "安全模式，跳过商品同步，未访问平台")
        return
    try:
        shops = run_with_retry(_list_enabled_shops)
    except Exception as exc:  # noqa: BLE001 —— 取店铺失败：记失败日志后返回
        logger.error("商品同步任务取店铺列表失败：%s", exc)
        task_run_log.write_failed(TASK_PRODUCT_SYNC, f"取店铺列表失败：{exc}")
        return

    if not shops:
        task_run_log.write_success(TASK_PRODUCT_SYNC, "无启用店铺，跳过商品同步")
        return

    success_count = 0
    failed_count = 0
    for shop_pk, shop_id, _owner_user_id in shops:
        result = service_client.trigger_product_sync(shop_pk)
        if result.ok:
            success_count += 1
        else:
            failed_count += 1
            logger.warning("店铺[%s] 商品同步失败：%s", shop_id, result.message)

    message = f"商品同步完成：成功 {success_count} 个，失败 {failed_count} 个"
    if failed_count > 0:
        task_run_log.write_failed(TASK_PRODUCT_SYNC, message)
    else:
        task_run_log.write_success(TASK_PRODUCT_SYNC, message)


def run_log_file_cleanup() -> None:
    """文件日志清理任务执行体（需求 21.4）。

    按系统设置的「日志保留天数」**仅清理磁盘日志文件**；数据库业务日志表禁止
    物理删除（规范 11 / 需求 19.5）。执行结果写一条执行日志。
    """
    try:
        retention_days = log_cleanup.resolve_retention_days()
        removed = log_cleanup.cleanup_log_files(retention_days)
        message = (
            f"文件日志清理完成：保留 {retention_days} 天，删除磁盘日志文件 "
            f"{len(removed)} 个（数据库业务日志未做任何删除）"
        )
        task_run_log.write_success(TASK_LOG_FILE_CLEANUP, message)
    except Exception as exc:  # noqa: BLE001 —— 清理失败：记失败日志，不抛出
        logger.error("文件日志清理任务失败：%s", exc)
        task_run_log.write_failed(TASK_LOG_FILE_CLEANUP, f"文件日志清理失败：{exc}")


# 任务键 -> 执行体 的映射，供 SchedulerService 按 scheduled_task.task_key 注册。
TASK_RUNNERS = {
    TASK_COOKIE_REFRESH: run_cookie_refresh,
    TASK_PRODUCT_SYNC: run_product_sync,
    TASK_LOG_FILE_CLEANUP: run_log_file_cleanup,
}


__all__ = [
    "run_cookie_refresh",
    "run_product_sync",
    "run_log_file_cleanup",
    "TASK_RUNNERS",
]
