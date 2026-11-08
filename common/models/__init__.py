# -*- coding: utf-8 -*-
"""
common.models 数据模型子包
==========================
本文件为 common 公共库 models 子包的初始化文件，集中导出系统全部数据表模型。

models 子包按业务域拆分为多个模型文件（单文件 ≤500 行，规范 35），全部模型
共享同一份统一声明式基类 ``Base``（见 base.py），便于启动自检迁移器
（SchemaMigrator，任务 2.12 / 4.1）通过 ``Base.metadata`` 统一建表。

业务域与文件组织：
- base.py             Base 基类 + 通用混入（IdMixin/TimestampMixin/AuditMixin）
- user_models.py      用户与权限：sys_user/sys_role/sys_permission/
                      sys_role_permission/sys_menu
- shop_models.py      渠道店铺账号：channel/shop/account
- reply_models.py     回复规则：keyword_rule/default_reply/goods_reply
- knowledge_models.py 知识库商品：product_knowledge/customer_service_knowledge/product
- config_models.py    配置控制：business_hours/message_filter_rule/blacklist/
                      risk_rule/transfer_keyword/llm_config
- log_models.py       会话日志：conversation/chat_message/message_log/risk_log/
                      system_log/notify_record
- setting_models.py   通知设置字典：notify_channel/sys_setting/sys_dict
- task_models.py      公告反馈定时任务：announcement/feedback/scheduled_task/task_run_log

设计约束（开发规范）：
- 规范 9：每张表统一自增 BIGINT 主键 ``id``。
- 规范 10：不使用外键约束，表间关系一律由代码维护。
- 规范 17：审计时间字段统一北京时间。
- 敏感字段（password_hash/cookies_enc/password_enc/api_key_enc 等）按设计命名，
  对外响应须脱敏。
"""
from __future__ import annotations

from common.models.base import (
    AuditMixin,
    Base,
    IdMixin,
    TimestampMixin,
)
from common.models.config_models import (
    Blacklist,
    BusinessHours,
    LlmConfig,
    MessageFilterRule,
    RiskRule,
    TransferKeyword,
)
from common.models.knowledge_models import (
    CustomerServiceKnowledge,
    Product,
    ProductKnowledge,
)
from common.models.log_models import (
    ChatMessage,
    Conversation,
    MessageLog,
    NotifyRecord,
    RiskLog,
    SystemLog,
)
from common.models.reply_dispatch import ReplyDispatch
from common.models.reply_models import (
    DefaultReply,
    DefaultReplyRecord,
    GoodsReply,
    KeywordRule,
)
from common.models.setting_models import (
    NotifyChannel,
    SysDict,
    SysSetting,
)
from common.models.shop_models import (
    Account,
    Channel,
    Shop,
)
from common.models.task_models import (
    Announcement,
    Feedback,
    ScheduledTask,
    TaskRunLog,
)
from common.models.user_models import (
    SysMenu,
    SysPermission,
    SysRole,
    SysRolePermission,
    SysUser,
)

__all__ = [
    # 基类与混入
    "Base",
    "IdMixin",
    "TimestampMixin",
    "AuditMixin",
    # 用户与权限
    "SysUser",
    "SysRole",
    "SysPermission",
    "SysRolePermission",
    "SysMenu",
    # 渠道、店铺与账号
    "Channel",
    "Shop",
    "Account",
    # 回复规则
    "KeywordRule",
    "ReplyDispatch",
    "DefaultReply",
    "DefaultReplyRecord",
    "GoodsReply",
    # 知识库与商品
    "ProductKnowledge",
    "CustomerServiceKnowledge",
    "Product",
    # 配置与控制
    "BusinessHours",
    "MessageFilterRule",
    "Blacklist",
    "RiskRule",
    "TransferKeyword",
    "LlmConfig",
    # 会话与日志
    "Conversation",
    "ChatMessage",
    "MessageLog",
    "RiskLog",
    "SystemLog",
    "NotifyRecord",
    # 通知、系统设置与字典
    "NotifyChannel",
    "SysSetting",
    "SysDict",
    # 公告、反馈与定时任务
    "Announcement",
    "Feedback",
    "ScheduledTask",
    "TaskRunLog",
]
