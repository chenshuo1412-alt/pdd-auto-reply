# -*- coding: utf-8 -*-
"""
backend.tests.test_ai_provider_models —— AI 模型列表获取单元测试
==============================================================
本文件用途：覆盖 common.services.ai_provider_service.fetch_model_list（需求 8，
「自动获取模型名称」）的核心场景：

- openai_compatible：解析 {data:[{id}]} 返回归一化 [{id,name}]。
- gemini：过滤仅支持 generateContent 的模型，去掉 models/ 前缀。
- dashscope_app：不支持自动获取，抛出 AiProviderError。
- 缺少 API 密钥：抛出 AiProviderError。

测试以打桩替换 urllib.request.urlopen，避免真实网络 IO。
"""
from __future__ import annotations

import io
import json

import pytest

from common.services import ai_provider_service
from common.services.ai_provider_service import AiProviderError, fetch_model_list


class _FakeResp:
    """模拟 urlopen 返回的上下文管理器响应。"""

    def __init__(self, payload: dict):
        self._raw = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._raw


def _stub_urlopen(monkeypatch, captured, payload):
    """打桩 urlopen：记录请求地址并返回固定 JSON 响应。"""

    def _fake(request, timeout=None):  # noqa: ANN001
        captured["url"] = request.full_url
        return _FakeResp(payload)

    monkeypatch.setattr(ai_provider_service.urllib.request, "urlopen", _fake)


def test_fetch_models_openai_compatible(monkeypatch):
    """openai 兼容：返回 data 列表并归一化为 [{id,name}]。"""
    captured: dict = {}
    _stub_urlopen(
        monkeypatch,
        captured,
        {"data": [{"id": "qwen-plus"}, {"id": "gpt-4o-mini"}, {"id": "qwen-plus"}]},
    )
    models = fetch_model_list("openai_compatible", "https://api.example.com/v1", "sk-x")
    ids = [m["id"] for m in models]
    # 去重保序
    assert ids == ["qwen-plus", "gpt-4o-mini"]
    assert captured["url"].endswith("/models")


def test_fetch_models_gemini_filters_unsupported(monkeypatch):
    """gemini：仅保留支持 generateContent 的模型，去 models/ 前缀。"""
    captured: dict = {}
    _stub_urlopen(
        monkeypatch,
        captured,
        {
            "models": [
                {"name": "models/gemini-1.5-pro", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/embedding-001", "supportedGenerationMethods": ["embedContent"]},
            ]
        },
    )
    models = fetch_model_list("gemini", "https://gemini.example/v1beta", "key-x")
    ids = [m["id"] for m in models]
    assert ids == ["gemini-1.5-pro"]


def test_fetch_models_dashscope_app_rejected():
    """dashscope_app 不支持自动获取模型列表。"""
    with pytest.raises(AiProviderError):
        fetch_model_list(
            "dashscope_app",
            "https://dashscope.aliyuncs.com/api/v1/apps/abc/completion",
            "key-x",
        )


def test_fetch_models_requires_api_key():
    """缺少 API 密钥时抛出 AiProviderError。"""
    with pytest.raises(AiProviderError):
        fetch_model_list("openai_compatible", "https://api.example.com/v1", "")
