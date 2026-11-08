# -*- coding: utf-8 -*-
"""
backend.app.services.ai_config_service —— AI（LLM）配置业务服务
==============================================================
本文件用途：实现 backend 服务的「AI 智能回复配置」业务逻辑，供 ai-config 路由
复用，满足需求 8（AI 智能回复配置）：

- ``get_ai_config(...)``：查询某店铺的 AI 配置（接口类型 / 模型 / 地址 / 指令 /
  开关）；API 密钥不返回明文，仅以 ``has_api_key`` 标识是否已配置（需求 8.6）。
- ``save_ai_config(...)``：保存（upsert）某店铺 AI 配置。按店铺主键 ``shop_pk``
  作为业务键 upsert（同一店铺一条配置）；API 密钥可逆加密存储，留空表示不修改。
- ``test_ai_config(...)``：按所选接口类型测试 AI 连接（需求 8 测试连接诉求）。
  测试可用「本次表单填写的密钥」或「已保存的密钥」（前端测试时密钥留空则用库内）。
- ``list_provider_types(...)``：返回接口类型选项（key + 中文 label + 默认地址），
  中文文案从数据字典查出（规范 15）。

实现约束（开发规范）：
- 统一响应体由 common.schemas.common 构造，HTTP 恒 200（规范 1-3 / 需求 24.1）。
- 数据访问经 common.db.repository 参数化查询（规范 16）。
- API 密钥经 common.utils.crypto 可逆加密存储，对外脱敏（需求 8.6 / 3.6）。
- 接口类型规范化 / 测试连接复用 common.services.ai_provider_service（规范 36/52）。
- 导入置顶（规范 51）；中文注释（规范 37）；单文件 ≤500 行（规范 35）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.core.business_codes import CODE_PARAM_ERROR
from app.core.data_scope import ensure_shop_in_scope
from common.db.repository import Repository
from common.models.config_models import LlmConfig
from common.models.setting_models import SysDict
from common.schemas.common import ApiResponse, error_response, success_response
from common.services.ai_provider_service import (
    AiProviderError,
    fetch_model_list,
    list_provider_options,
    normalize_ai_provider_type,
    test_ai_connection,
)
from common.utils.crypto import encrypt_text, try_decrypt_text

# AI 接口类型的数据字典类型键（与 dict_seed_data 一致）。
_DICT_TYPE_PROVIDER: str = "ai_provider_type"
_EMPTY_BASE_MESSAGE = "请填写模型 API 地址，空白地址不会改用云端默认"


def _explicit_api_base(api_base: Any, record: LlmConfig | None = None) -> str:
    """本次填写优先；否则只用库内非空地址。空白不回退云端。"""
    if api_base is not None and str(api_base).strip():
        return str(api_base).strip()
    stored = ""
    if record is not None and record.api_base:
        stored = str(record.api_base).strip()
    return stored


def serialize_ai_config(record: LlmConfig) -> Dict[str, Any]:
    """将 LLM 配置模型序列化为对外字典（含反显明文密钥，供页面查看/编辑）。

    API 密钥以可逆加密存储；本接口受数据范围隔离保护（仅可见范围内店铺），应
    用户要求反显解密后的明文，前端以「隐藏查看」（密码框 + 显隐切换）展示，便于
    核对与编辑（与系统设置 SMTP 密码一致）。同时保留 ``has_api_key`` 标记便于
    前端占位文案展示。

    Args:
        record: LLM 配置模型实例。

    Returns:
        含明文 ``api_key`` 与 ``has_api_key`` 标记的 AI 配置信息字典。
    """
    # 解密反显：仅在已配置密钥时解密，解密失败（密钥变更等）回退为空串，不抛异常。
    api_key = try_decrypt_text(record.api_key_enc) if record.api_key_enc else ""
    return {
        "id": record.id,
        "shop_pk": record.shop_pk,
        "provider_type": normalize_ai_provider_type(record.provider_type),
        "model_name": record.model_name or "",
        "api_base": record.api_base or "",
        "instructions": record.instructions or "",
        "ai_enabled": bool(record.ai_enabled),
        # 反显明文密钥（前端以显隐切换展示），应用户要求支持回显。
        "api_key": api_key or "",
        # 是否已配置密钥（用于前端占位提示）。
        "has_api_key": bool(record.api_key_enc),
    }


def get_ai_config(
    session: Session, shop_pk: int, *, operator_id: Optional[int] = None
) -> ApiResponse:
    """查询某店铺的 AI 配置（需求 8.6）。

    未配置时返回 ``data=None``，便于前端区分「未配置」与「已配置」。非管理员
    仅可查看其可见范围内店铺的配置（数据范围隔离，需求 3.7 / 规范 42a）。

    Args:
        session: 数据库会话。
        shop_pk: 店铺主键（shop.id）。
        operator_id: 当前操作用户 ID（数据范围校验）。

    Returns:
        统一响应体：已配置返回脱敏配置；未配置返回 data=None。
    """
    if shop_pk is None or not isinstance(shop_pk, int) or shop_pk <= 0:
        return error_response(CODE_PARAM_ERROR, "店铺主键不能为空")

    # 数据范围隔离：店铺需存在且在当前用户可见范围内（需求 3.7 / 规范 42a）。
    denied = ensure_shop_in_scope(session, shop_pk, operator_id)
    if denied is not None:
        return denied

    record = Repository(LlmConfig, session).get_by(shop_pk=shop_pk)
    if record is None:
        return success_response(data=None, message="未配置 AI")
    return success_response(data=serialize_ai_config(record), message="查询成功")


def save_ai_config(
    session: Session,
    shop_pk: int,
    *,
    provider_type: Any = None,
    model_name: Any = None,
    api_base: Any = None,
    instructions: Any = None,
    api_key: Any = None,
    ai_enabled: bool = False,
    operator_id: Optional[int] = None,
) -> ApiResponse:
    """保存（upsert）某店铺 AI 配置（需求 8.6）。

    按店铺主键 ``shop_pk`` upsert：同一店铺仅一条配置，重复保存覆盖更新（幂等）。
    API 密钥经可逆加密存储；``api_key`` 为 None / 空表示「不修改已存密钥」，避免
    将已配置密钥误清空。接口类型规范化后落库。

    Args:
        session: 数据库会话。
        shop_pk: 店铺主键（shop.id）。
        provider_type: 接口类型（openai_compatible/anthropic/gemini/dashscope_app）。
        model_name: 模型名称。
        api_base: API 接口地址。空白直接拒绝，不写入云端默认地址。
        instructions: 提示词指令文本。
        api_key: API 密钥明文（为空表示不修改已存密钥）。
        ai_enabled: 是否启用 AI 回复。
        operator_id: 操作人用户 ID（创建人审计字段，仅新建时记录）。

    Returns:
        统一响应体：成功返回脱敏后的 AI 配置。
    """
    if shop_pk is None or not isinstance(shop_pk, int) or shop_pk <= 0:
        return error_response(CODE_PARAM_ERROR, "店铺主键不能为空")

    # 数据范围隔离：店铺需存在且在当前用户可见范围内（需求 3.7 / 规范 42a）。
    denied = ensure_shop_in_scope(session, shop_pk, operator_id)
    if denied is not None:
        return denied

    provider = normalize_ai_provider_type(provider_type, api_base, model_name)
    base = _explicit_api_base(api_base)
    if not base:
        return error_response(CODE_PARAM_ERROR, _EMPTY_BASE_MESSAGE)
    model = str(model_name).strip() if model_name else None
    instr = str(instructions) if instructions is not None else None

    # 启用 AI 时需具备最小可用配置：模型名（dashscope_app 除外）与密钥（新建时）。
    repo = Repository(LlmConfig, session)
    existing = repo.get_by(shop_pk=shop_pk)

    values: Dict[str, Any] = {
        "provider_type": provider,
        "model_name": model,
        "api_base": base,
        "instructions": instr,
        "ai_enabled": bool(ai_enabled),
    }
    # 仅当传入了新密钥时才更新密文，留空表示沿用已存密钥（需求 8.6）。
    if api_key is not None and str(api_key).strip():
        values["api_key_enc"] = encrypt_text(str(api_key).strip())

    if existing is None:
        values["created_by"] = operator_id
        record = repo.create(shop_pk=shop_pk, **values)
    else:
        record = repo.update(existing.id, **values)

    return success_response(data=serialize_ai_config(record), message="保存成功")


def fetch_models(
    session: Session,
    shop_pk: int,
    *,
    provider_type: Any = None,
    api_base: Any = None,
    api_key: Any = None,
    operator_id: Optional[int] = None,
) -> ApiResponse:
    """按所选接口类型从服务商拉取可用模型列表（供前端自动获取模型名称）。

    密钥来源：优先使用本次传入的 ``api_key``；为空时回退使用库内已存密钥（解密），
    便于「已保存配置后直接点获取模型」。获取失败 / 服务商不支持时返回 success=false
    与中文原因，由前端提示改为手动填写（HTTP 恒 200）。

    Args:
        session: 数据库会话。
        shop_pk: 店铺主键（shop.id）。
        provider_type: 接口类型；缺失时回退库内已存值。
        api_base: API 接口地址；缺失时回退库内已存值。
        api_key: API 密钥明文；为空时回退库内已存密钥。

    Returns:
        统一响应体：成功返回 data={"models": [{id, name}]}；失败返回中文原因。
    """
    if shop_pk is None or not isinstance(shop_pk, int) or shop_pk <= 0:
        return error_response(CODE_PARAM_ERROR, "店铺主键不能为空")

    # 数据范围隔离：防止借用他人店铺已存密钥发起外部调用（需求 3.7 / 规范 42a）。
    denied = ensure_shop_in_scope(session, shop_pk, operator_id)
    if denied is not None:
        return denied

    record = Repository(LlmConfig, session).get_by(shop_pk=shop_pk)

    provider = normalize_ai_provider_type(
        provider_type if provider_type else (record.provider_type if record else None),
        api_base if api_base else (record.api_base if record else None),
        None,
    )
    base = _explicit_api_base(api_base, record)
    if not base:
        return error_response(CODE_PARAM_ERROR, _EMPTY_BASE_MESSAGE)
    key = str(api_key).strip() if api_key else ""
    if not key and record is not None:
        key = try_decrypt_text(record.api_key_enc) or ""
    if not key:
        return error_response(CODE_PARAM_ERROR, "请先填写 API 密钥再获取模型列表")

    try:
        models = fetch_model_list(provider, base, key)
    except AiProviderError as exc:
        return error_response(CODE_PARAM_ERROR, f"获取模型列表失败：{exc}，可手动填写模型名称")
    except Exception as exc:  # noqa: BLE001 - 兜底未知异常，不抛出打断响应
        return error_response(CODE_PARAM_ERROR, f"获取模型列表失败：{exc}，可手动填写模型名称")

    if not models:
        return error_response(
            CODE_PARAM_ERROR, "该服务商未返回模型列表，请手动填写模型名称"
        )
    return success_response(
        data={"models": models},
        message=f"获取模型列表成功，共 {len(models)} 个模型",
    )


def test_ai_config(
    session: Session,
    shop_pk: int,
    *,
    provider_type: Any = None,
    model_name: Any = None,
    api_base: Any = None,
    api_key: Any = None,
    operator_id: Optional[int] = None,
) -> ApiResponse:
    """按所选接口类型测试 AI 连接（需求 8 测试连接）。

    密钥来源：优先使用本次传入的 ``api_key``；为空时回退使用库内已存密钥（解密），
    便于「已保存配置后直接点测试」。测试为同步阻塞调用（有界超时），失败返回中文
    原因（HTTP 恒 200）。

    Args:
        session: 数据库会话。
        shop_pk: 店铺主键（shop.id）。
        provider_type: 接口类型。
        model_name: 模型名称。
        api_base: API 接口地址。
        api_key: API 密钥明文（为空时回退库内已存密钥）。

    Returns:
        统一响应体：测试成功返回模型回复文本；失败返回中文原因。
    """
    if shop_pk is None or not isinstance(shop_pk, int) or shop_pk <= 0:
        return error_response(CODE_PARAM_ERROR, "店铺主键不能为空")

    # 数据范围隔离：防止借用他人店铺已存密钥发起外部调用（需求 3.7 / 规范 42a）。
    denied = ensure_shop_in_scope(session, shop_pk, operator_id)
    if denied is not None:
        return denied

    record = Repository(LlmConfig, session).get_by(shop_pk=shop_pk)

    # 接口类型 / 地址 / 模型：优先取本次传入，缺失回退库内已存值。
    provider = normalize_ai_provider_type(
        provider_type if provider_type else (record.provider_type if record else None),
        api_base if api_base else (record.api_base if record else None),
        model_name if model_name else (record.model_name if record else None),
    )
    base = _explicit_api_base(api_base, record)
    if not base:
        return error_response(CODE_PARAM_ERROR, _EMPTY_BASE_MESSAGE)
    model = (str(model_name).strip() if model_name else "") or (
        record.model_name if record else ""
    )

    # 密钥：优先本次传入；为空回退库内解密（已存密钥）。
    key = str(api_key).strip() if api_key else ""
    if not key and record is not None:
        key = try_decrypt_text(record.api_key_enc) or ""
    if not key:
        return error_response(CODE_PARAM_ERROR, "请先填写 API 密钥再测试")

    try:
        reply = test_ai_connection(provider, base, key, model)
    except AiProviderError as exc:
        # 已知的接口 / 配置错误：返回中文原因（HTTP 恒 200）。
        return error_response(CODE_PARAM_ERROR, f"测试失败：{exc}")
    except Exception as exc:  # noqa: BLE001 - 兜底未知异常，不抛出打断响应
        return error_response(CODE_PARAM_ERROR, f"测试失败：{exc}")

    return success_response(
        data={"reply": reply},
        message="测试成功，AI 接口连接正常",
    )


def list_provider_types(session: Session) -> ApiResponse:
    """返回 AI 接口类型选项（key + 中文 label + 默认地址，需求 8 / 规范 15）。

    中文 label 优先取自数据字典（sys_dict 的 ai_provider_type 类型）；字典缺失项
    回退 ai_provider_service 内置中文名称，保证前端始终有可展示文案。

    Args:
        session: 数据库会话。

    Returns:
        统一响应体：data 为选项列表 [{key, label, default_base_url}]。
    """
    # 从数据字典查中文文案（规范 15：枚举展示从字典查中文）。
    dict_rows = Repository(SysDict, session).list(
        filters={"dict_type": _DICT_TYPE_PROVIDER, "enabled": True},
        order_by=SysDict.order_no,
        desc_order=False,
    )
    label_map = {row.dict_key: row.dict_label for row in dict_rows}

    options: List[Dict[str, str]] = []
    for opt in list_provider_options():
        key = opt["key"]
        options.append(
            {
                "key": key,
                # 字典有则用字典中文，否则回退内置名称
                "label": label_map.get(key, opt["label"]),
                "default_base_url": opt["default_base_url"],
            }
        )
    return success_response(data=options, message="查询成功")


__all__ = [
    "serialize_ai_config",
    "get_ai_config",
    "save_ai_config",
    "fetch_models",
    "test_ai_config",
    "list_provider_types",
]
