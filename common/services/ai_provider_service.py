# -*- coding: utf-8 -*-
"""
common.services.ai_provider_service —— AI 服务商工具
====================================================
本文件用途：为「拼多多自动回复」系统提供统一的 AI 服务商（接口类型）识别、
默认地址、配置校验与「测试连接」能力，供 backend 的 AI 设置接口复用，满足
需求 8（AI 智能回复配置）中「选择接口类型 + 测试连接」的诉求。

支持的接口类型（provider_type，枚举入 sys_dict —— 规范 15）：
- ``openai_compatible``：OpenAI 兼容协议（OpenAI / 通义 / DeepSeek / 智谱 / Kimi 等
  绝大多数国产与三方中转服务，最常用）。
- ``anthropic``：Anthropic Claude 官方协议。
- ``gemini``：Google Gemini 官方协议。
- ``dashscope_app``：阿里云百炼 DashScope 应用 API。

设计要点：
- **仅依赖标准库 urllib**：common 运行期无 httpx，与 ``service_client`` 口径一致，
  避免为各服务引入额外 HTTP 依赖。测试连接为同步阻塞调用，调用方自行控制超时。
- **纯函数与网络分离**：类型规范化 / 名称识别 / 配置校验为不依赖网络的纯函数，
  便于单测；``test_ai_connection`` 负责实际网络探测。
- **健壮性**：测试连接对网络 / 协议错误统一抛出带中文原因的 ``AiProviderError``，
  由调用方规整为统一响应体（HTTP 恒 200，规范 1-3）。

实现约束（开发规范）：导入置顶（规范 51）、中文注释（规范 37）、文件名用下划线
（规范 40）、单文件 ≤500 行（规范 35）、日志禁用 debug（规范 38）。
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Dict, List

logger = logging.getLogger("common.ai_provider")

# 默认 API 地址（OpenAI 兼容默认指向阿里云百炼兼容模式，国内可达且常用）。
DEFAULT_AI_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
# 默认接口类型。
DEFAULT_AI_PROVIDER_TYPE: str = "openai_compatible"

# 合法接口类型集合。
VALID_AI_PROVIDER_TYPES: frozenset[str] = frozenset(
    {"openai_compatible", "anthropic", "gemini", "dashscope_app"}
)

# 接口类型 -> 中文名称（供前端展示 / 数据字典登记，规范 15 / 27）。
AI_PROVIDER_NAMES: Dict[str, str] = {
    "openai_compatible": "OpenAI 兼容",
    "anthropic": "Anthropic Claude",
    "gemini": "Google Gemini",
    "dashscope_app": "DashScope 应用",
}

# 接口类型 -> 默认 API 地址。
AI_PROVIDER_DEFAULT_BASE_URLS: Dict[str, str] = {
    "openai_compatible": DEFAULT_AI_BASE_URL,
    "anthropic": "https://api.anthropic.com",
    "gemini": "https://generativelanguage.googleapis.com",
    "dashscope_app": "https://dashscope.aliyuncs.com/api/v1/apps/{app_id}/completion",
}

# 测试连接默认超时（秒）。
TEST_CONNECTION_TIMEOUT: float = 30.0


class AiProviderError(Exception):
    """AI 服务商调用异常：携带中文原因，供上层规整为失败响应体。"""


def clean_ai_text(value: Any) -> str:
    """清理 AI 配置文本中的危险换行与首尾空白（防注入与误配）。"""
    return str(value or "").replace("\r", "").replace("\n", "").strip()


def normalize_ai_provider_type(
    provider_type: Any = None, base_url: Any = "", model_name: Any = ""
) -> str:
    """规范化接口类型，兼容别名与历史无此字段的旧配置。

    优先按显式 provider_type（及其别名）判定；缺失时按 base_url / model_name 特征
    推断；都无法判定时回退默认 ``openai_compatible``。

    Args:
        provider_type: 显式接口类型（可能为别名 / 旧值）。
        base_url: API 地址（用于特征推断）。
        model_name: 模型名（用于特征推断）。

    Returns:
        规范化后的接口类型（VALID_AI_PROVIDER_TYPES 之一）。
    """
    provider = clean_ai_text(provider_type).lower().replace("-", "_")
    aliases = {
        "openai": "openai_compatible",
        "openai_compatible": "openai_compatible",
        "qwen": "openai_compatible",
        "dashscope_compatible": "openai_compatible",
        "anthropic": "anthropic",
        "claude": "anthropic",
        "gemini": "gemini",
        "google_gemini": "gemini",
        "dashscope_app": "dashscope_app",
    }
    if provider in aliases:
        return aliases[provider]
    if provider in VALID_AI_PROVIDER_TYPES:
        return provider

    base = clean_ai_text(base_url).lower()
    model = clean_ai_text(model_name).lower()
    if "dashscope.aliyuncs.com" in base and "/apps/" in base:
        return "dashscope_app"
    if "generativelanguage.googleapis.com" in base:
        return "gemini"
    if "api.anthropic.com" in base:
        return "anthropic"
    if not base and "gemini" in model:
        return "gemini"
    if not base and "claude" in model:
        return "anthropic"
    return DEFAULT_AI_PROVIDER_TYPE


def get_ai_provider_label(provider_type: Any = None) -> str:
    """返回接口类型的中文名称（用于展示）。"""
    provider = normalize_ai_provider_type(provider_type)
    return AI_PROVIDER_NAMES.get(provider, AI_PROVIDER_NAMES[DEFAULT_AI_PROVIDER_TYPE])


def get_default_ai_base_url(provider_type: Any = None) -> str:
    """返回某接口类型的默认 API 地址。"""
    provider = normalize_ai_provider_type(provider_type)
    return AI_PROVIDER_DEFAULT_BASE_URLS.get(provider, DEFAULT_AI_BASE_URL)


def list_provider_options() -> List[Dict[str, str]]:
    """返回接口类型选项列表（key + 中文 label + 默认地址），供前端下拉与字典登记。"""
    return [
        {
            "key": key,
            "label": AI_PROVIDER_NAMES[key],
            "default_base_url": AI_PROVIDER_DEFAULT_BASE_URLS.get(key, ""),
        }
        for key in ("openai_compatible", "anthropic", "gemini", "dashscope_app")
    ]


def get_ai_settings_missing_fields(settings: Dict[str, Any] | None) -> List[str]:
    """返回启用 / 测试 AI 前必须补全的字段中文名列表（为空表示已完整）。

    Args:
        settings: 含 provider_type / base_url / api_key / model_name 的配置字典。

    Returns:
        缺失字段的中文名列表（如 ["API 地址", "API 密钥"]）。
    """
    payload = dict(settings or {})
    provider = normalize_ai_provider_type(
        payload.get("provider_type"),
        payload.get("base_url"),
        payload.get("model_name"),
    )
    base_url = clean_ai_text(payload.get("base_url"))
    api_key = clean_ai_text(payload.get("api_key"))
    model_name = clean_ai_text(payload.get("model_name"))

    missing: List[str] = []
    if not api_key:
        missing.append("API 密钥")
    if provider != "dashscope_app" and not model_name:
        missing.append("模型名称")
    if provider == "dashscope_app" and ("{app_id}" in base_url or "/apps/" not in base_url):
        missing.append("DashScope 应用地址")
    return missing


def normalize_openai_base_url(base_url: Any) -> str:
    """规范化 OpenAI 兼容接口基础地址。空地址拒绝，不再回退到云端默认。"""
    base = clean_ai_text(base_url)
    if not base:
        raise AiProviderError("未配置 API 地址，已拒绝访问默认云端模型")
    base = base.rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    if base.endswith("/models"):
        base = base[: -len("/models")]
    return base


def _build_anthropic_url(base_url: Any, path: str) -> str:
    """拼接 Anthropic 接口地址。"""
    base = (clean_ai_text(base_url) or AI_PROVIDER_DEFAULT_BASE_URLS["anthropic"]).rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/{path.lstrip('/')}"
    return f"{base}/v1/{path.lstrip('/')}"


def _build_gemini_url(base_url: Any, path: str) -> str:
    """拼接 Gemini 接口地址。"""
    base = (clean_ai_text(base_url) or AI_PROVIDER_DEFAULT_BASE_URLS["gemini"]).rstrip("/")
    if base.endswith("/v1beta") or base.endswith("/v1"):
        return f"{base}/{path.lstrip('/')}"
    return f"{base}/v1beta/{path.lstrip('/')}"


def _http_post_json(
    url: str, headers: Dict[str, str], payload: Dict[str, Any], timeout: float
) -> Dict[str, Any]:
    """发起 POST(JSON) 请求并返回解析后的字典，异常统一转 ``AiProviderError``。

    Args:
        url: 完整请求地址。
        headers: 请求头。
        payload: 请求体（将序列化为 JSON）。
        timeout: 超时秒数。

    Returns:
        解析后的响应字典。

    Raises:
        AiProviderError: 网络不可达 / 超时 / 非 2xx / 响应非 JSON。
    """
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        # 非 2xx：尽力提取错误体中的中文 / 英文原因。
        detail = _extract_http_error(exc)
        raise AiProviderError(f"接口返回错误（HTTP {exc.code}）：{detail}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise AiProviderError(f"无法连接 AI 接口：{exc}") from exc

    try:
        parsed = json.loads(raw) if raw else {}
    except ValueError as exc:
        raise AiProviderError("AI 接口返回格式异常（非 JSON）") from exc
    if not isinstance(parsed, dict):
        raise AiProviderError("AI 接口返回格式异常")
    return parsed


def _extract_http_error(exc: urllib.error.HTTPError) -> str:
    """从 HTTPError 响应体中提取简短错误原因（截断 300 字）。"""
    try:
        raw = exc.read().decode("utf-8")
    except Exception:  # noqa: BLE001 - 读取错误体失败时回退状态码描述
        return f"HTTP {exc.code}"
    if not raw:
        return f"HTTP {exc.code}"
    try:
        body = json.loads(raw)
    except ValueError:
        return raw[:300]
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("type") or error)[:300]
        if isinstance(error, str):
            return error[:300]
        for key in ("message", "msg", "detail"):
            if body.get(key):
                return str(body[key])[:300]
    return str(body)[:300]


def _http_get_json(
    url: str, headers: Dict[str, str], timeout: float
) -> Dict[str, Any]:
    """发起 GET(JSON) 请求并返回解析后的字典，异常统一转 ``AiProviderError``。

    用于「获取模型列表」等只读探测；与 ``_http_post_json`` 口径一致，仅依赖标准库。

    Args:
        url: 完整请求地址。
        headers: 请求头。
        timeout: 超时秒数。

    Returns:
        解析后的响应字典（部分接口返回数组时包装为 {"data": [...]}）。

    Raises:
        AiProviderError: 网络不可达 / 超时 / 非 2xx / 响应非 JSON。
    """
    request = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = _extract_http_error(exc)
        raise AiProviderError(f"接口返回错误（HTTP {exc.code}）：{detail}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise AiProviderError(f"无法连接 AI 接口：{exc}") from exc

    try:
        parsed = json.loads(raw) if raw else {}
    except ValueError as exc:
        raise AiProviderError("AI 接口返回格式异常（非 JSON）") from exc
    # 兼容返回顶层数组的接口（部分中转服务直接返回模型数组）。
    if isinstance(parsed, list):
        return {"data": parsed}
    if not isinstance(parsed, dict):
        raise AiProviderError("AI 接口返回格式异常")
    return parsed


def _normalize_model_options(models: List[Any]) -> List[Dict[str, str]]:
    """将各服务商返回的模型条目归一化为 [{id, name}]（去重、去 models/ 前缀）。

    Args:
        models: 服务商返回的模型条目列表（每项通常为 dict）。

    Returns:
        归一化后的模型选项列表 [{id, name}]，按出现顺序去重。
    """
    seen: set[str] = set()
    options: List[Dict[str, str]] = []
    for model in models:
        if isinstance(model, str):
            model_id = clean_ai_text(model)
            display = model_id
        elif isinstance(model, dict):
            model_id = clean_ai_text(model.get("id") or model.get("name") or model.get("model"))
            display = clean_ai_text(
                model.get("display_name") or model.get("displayName") or model.get("name")
            )
        else:
            continue
        # Gemini 模型名形如 "models/gemini-1.5-pro"，去掉前缀。
        if model_id.startswith("models/"):
            model_id = model_id.split("/", 1)[1]
        if display.startswith("models/"):
            display = model_id
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        options.append({"id": model_id, "name": display or model_id})
    return options


def fetch_model_list(
    provider_type: Any,
    base_url: Any,
    api_key: Any,
    *,
    timeout: float = TEST_CONNECTION_TIMEOUT,
) -> List[Dict[str, str]]:
    """按接口类型从服务商拉取可用模型列表，返回 [{id, name}]。

    支持 openai_compatible（GET /models）、anthropic（GET /v1/models）、
    gemini（GET /v1beta/models?key=）；dashscope_app 不支持自动获取，抛出
    ``AiProviderError`` 由调用方提示手动填写。

    Args:
        provider_type: 接口类型。
        base_url: API 地址。
        api_key: API 密钥（必填）。
        timeout: 超时秒数。

    Returns:
        归一化的模型选项列表 [{id, name}]（可能为空，表示该服务商未返回）。

    Raises:
        AiProviderError: 密钥缺失、不支持的服务商或接口调用失败。
    """
    provider = normalize_ai_provider_type(provider_type, base_url, "")
    key = clean_ai_text(api_key)
    if not key:
        raise AiProviderError("请先填写 API 密钥再获取模型列表")
    if provider == "dashscope_app":
        raise AiProviderError("DashScope 应用接口不支持自动获取模型列表，请手动填写模型名称")

    if provider == "anthropic":
        url = _build_anthropic_url(base_url, "/models")
        headers = {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        result = _http_get_json(url, headers, timeout)
        data = result.get("data", []) if isinstance(result, dict) else []
        return _normalize_model_options(data if isinstance(data, list) else [])

    if provider == "gemini":
        # 经 x-goog-api-key 请求头传递密钥，避免密钥出现在 URL 查询串而随访问
        # 日志 / 异常信息泄漏（也无需对密钥做 URL 编码）。
        url = _build_gemini_url(base_url, "/models")
        result = _http_get_json(
            url,
            {"Content-Type": "application/json", "x-goog-api-key": key},
            timeout,
        )
        data = result.get("models", []) if isinstance(result, dict) else []
        if isinstance(data, list):
            # 仅保留支持 generateContent 的模型（可用于对话）。
            data = [
                item
                for item in data
                if not isinstance(item, dict)
                or "supportedGenerationMethods" not in item
                or "generateContent" in item.get("supportedGenerationMethods", [])
            ]
        return _normalize_model_options(data if isinstance(data, list) else [])

    # openai_compatible：GET {base}/models
    url = f"{normalize_openai_base_url(base_url)}/models"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    result = _http_get_json(url, headers, timeout)
    if isinstance(result, dict):
        data = result.get("data") or result.get("models") or []
    else:
        data = []
    return _normalize_model_options(data if isinstance(data, list) else [])


def test_ai_connection(
    provider_type: Any,
    base_url: Any,
    api_key: Any,
    model_name: Any,
    *,
    timeout: float = TEST_CONNECTION_TIMEOUT,
) -> str:
    """按接口类型测试 AI 连接，成功返回模型的测试回复文本。

    发送一条极简对话探测接口可用性与凭据正确性。任一环节失败抛出
    ``AiProviderError``（含中文原因）。

    Args:
        provider_type: 接口类型。
        base_url: API 地址。
        api_key: API 密钥。
        model_name: 模型名称（dashscope_app 可不填）。
        timeout: 超时秒数。

    Returns:
        模型返回的测试文本（用于前端展示「测试成功，模型回复：...」）。

    Raises:
        AiProviderError: 配置不完整或接口调用失败。
    """
    raw_settings = {
        "provider_type": provider_type,
        "base_url": base_url,
        "api_key": api_key,
        "model_name": model_name,
    }
    missing = get_ai_settings_missing_fields(raw_settings)
    if missing:
        raise AiProviderError(f"配置不完整，请先补全：{'、'.join(missing)}")

    provider = normalize_ai_provider_type(provider_type, base_url, model_name)
    key = clean_ai_text(api_key)
    model = clean_ai_text(model_name)
    prompt = "你好，请回复『测试成功』"

    if provider == "anthropic":
        return _test_anthropic(base_url, key, model, prompt, timeout)
    if provider == "gemini":
        return _test_gemini(base_url, key, model, prompt, timeout)
    if provider == "dashscope_app":
        return _test_dashscope_app(base_url, key, prompt, timeout)
    return _test_openai_compatible(base_url, key, model, prompt, timeout)


def _test_openai_compatible(
    base_url: Any, key: str, model: str, prompt: str, timeout: float
) -> str:
    """测试 OpenAI 兼容接口（/chat/completions）。"""
    url = f"{normalize_openai_base_url(base_url)}/chat/completions"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 50,
        "temperature": 0.5,
    }
    result = _http_post_json(url, headers, payload, timeout)
    try:
        return clean_ai_text(result["choices"][0]["message"]["content"]) or "测试成功"
    except (KeyError, IndexError, TypeError) as exc:
        raise AiProviderError(f"接口响应格式异常：{str(result)[:200]}") from exc


def _test_anthropic(
    base_url: Any, key: str, model: str, prompt: str, timeout: float
) -> str:
    """测试 Anthropic Claude 接口（/messages）。"""
    url = _build_anthropic_url(base_url, "/messages")
    headers = {
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": 50,
        "temperature": 0.5,
        "messages": [{"role": "user", "content": prompt}],
    }
    result = _http_post_json(url, headers, payload, timeout)
    content = result.get("content", []) if isinstance(result, dict) else []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
            return clean_ai_text(item["text"]) or "测试成功"
    raise AiProviderError(f"接口响应格式异常：{str(result)[:200]}")


def _test_gemini(
    base_url: Any, key: str, model: str, prompt: str, timeout: float
) -> str:
    """测试 Google Gemini 接口（generateContent）。"""
    # 经 x-goog-api-key 请求头传递密钥，避免密钥出现在 URL 查询串而泄漏。
    url = _build_gemini_url(base_url, f"/models/{model}:generateContent")
    headers = {"Content-Type": "application/json", "x-goog-api-key": key}
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.5, "maxOutputTokens": 50},
    }
    result = _http_post_json(url, headers, payload, timeout)
    try:
        return clean_ai_text(
            result["candidates"][0]["content"]["parts"][0]["text"]
        ) or "测试成功"
    except (KeyError, IndexError, TypeError) as exc:
        raise AiProviderError(f"接口响应格式异常：{str(result)[:200]}") from exc


def _test_dashscope_app(base_url: Any, key: str, prompt: str, timeout: float) -> str:
    """测试 DashScope 应用接口（/apps/{app_id}/completion）。"""
    base = clean_ai_text(base_url)
    if "/apps/" not in base:
        raise AiProviderError("DashScope 应用地址中未找到 app_id")
    app_id = base.split("/apps/", 1)[1].split("/", 1)[0]
    url = f"https://dashscope.aliyuncs.com/api/v1/apps/{app_id}/completion"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {
        "input": {"prompt": prompt},
        "parameters": {"max_tokens": 50, "temperature": 0.5},
        "debug": {},
    }
    result = _http_post_json(url, headers, payload, timeout)
    try:
        return clean_ai_text(result["output"]["text"]) or "测试成功"
    except (KeyError, TypeError) as exc:
        raise AiProviderError(f"接口响应格式异常：{str(result)[:200]}") from exc


__all__ = [
    "DEFAULT_AI_BASE_URL",
    "DEFAULT_AI_PROVIDER_TYPE",
    "VALID_AI_PROVIDER_TYPES",
    "AI_PROVIDER_NAMES",
    "AI_PROVIDER_DEFAULT_BASE_URLS",
    "AiProviderError",
    "clean_ai_text",
    "normalize_ai_provider_type",
    "get_ai_provider_label",
    "get_default_ai_base_url",
    "list_provider_options",
    "get_ai_settings_missing_fields",
    "normalize_openai_base_url",
    "fetch_model_list",
    "test_ai_connection",
]
