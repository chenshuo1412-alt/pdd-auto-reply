# -*- coding: utf-8 -*-
"""保存和测试 AI 配置时，空白地址不得写成云端默认。"""
from __future__ import annotations

import pytest

from app.services import ai_config_service
from common.db.repository import Repository
from common.models.config_models import LlmConfig


@pytest.fixture(autouse=True)
def _allow_shop(monkeypatch):
    monkeypatch.setattr(
        "app.services.ai_config_service.ensure_shop_in_scope",
        lambda *_args, **_kwargs: None,
    )


def test_blank_address_is_not_saved_as_cloud_default(db_session, monkeypatch):
    hits = []

    def urlopen(*_args, **_kwargs):
        hits.append(True)
        raise AssertionError("blank address must not egress")

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    rejected = ai_config_service.save_ai_config(
        db_session,
        1,
        provider_type="openai_compatible",
        model_name="local-model",
        api_base="  ",
        api_key="synthetic-key",
    )
    assert rejected.success is False
    assert "云端" in rejected.message
    assert Repository(LlmConfig, db_session).get_by(shop_pk=1) is None

    Repository(LlmConfig, db_session).create(
        shop_pk=1,
        provider_type="anthropic",
        model_name="old-model",
        api_base=None,
        ai_enabled=True,
    )
    tested = ai_config_service.test_ai_config(db_session, 1, api_base="", api_key="synthetic-key", model_name="old-model")
    listed = ai_config_service.fetch_models(db_session, 1, api_base=None, api_key="synthetic-key")
    assert tested.success is False
    assert listed.success is False
    record = Repository(LlmConfig, db_session).get_by(shop_pk=1)
    assert not (record.api_base or "").strip()
    assert hits == []


def test_explicit_cloud_url_is_kept(db_session):
    saved = ai_config_service.save_ai_config(
        db_session,
        2,
        provider_type="openai_compatible",
        model_name="qwen-plus",
        api_base="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key="synthetic-key",
    )
    assert saved.success is True
    assert saved.data["api_base"] == "https://dashscope.aliyuncs.com/compatible-mode/v1"
