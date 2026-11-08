# -*- coding: utf-8 -*-
"""
websocket.agent.llm_client —— LLM 客户端封装（多协议）
======================================================
本文件用途：封装与大语言模型 API 的交互，供 websocket 服务 AI 回复引擎调用。
按接口类型（provider_type）分流，覆盖四种服务商协议（需求 8）：

- ``openai_compatible``：经 ``AsyncOpenAI`` 调用，支持工具（function calling）
  调用，配合知识库工具实现检索增强；
- ``anthropic`` / ``gemini`` / ``dashscope_app``：经 ``httpx`` 按各服务商官方协议
  发起「纯对话」调用（不含工具调用，直接返回文本），覆盖 Claude / Gemini /
  阿里云百炼应用。

并提供与框架解耦的纯数据响应结构（``LLMResponse`` / ``ToolCall``），使 Agent
循环逻辑可在不依赖具体 SDK 的情况下被单元测试覆盖。

关键约束（开发规范 + 需求 8）：
- API 密钥 / 地址 / 模型名 / 接口类型不写死，由 AgentConfig（来自数据库
  LlmConfig）注入（需求 8.6、规范 21）；
- 单次调用设置超时（与 AgentConfig.timeout_seconds 对齐），超时 / 失败由上层
  Agent 循环捕获并回退默认回复（需求 8.5）；
- AI 回复内容使用中文（需求 8.7，由系统 Prompt 与指令约束，本模块不改写内容）；
- 不在日志中回显密钥 / 完整消息内容，避免敏感信息外泄（规范 37 / 需求 8.6）。

说明：非 OpenAI 兼容协议的工具调用格式各异（Claude/Gemini 不同、DashScope 应用
不支持），为保证可靠性，这三类走纯对话调用；客服场景下「系统指令 + 店铺指令 +
对话」已足以应答，知识库工具检索为 OpenAI 兼容协议下的增强能力。
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ToolFunction:
    """工具调用的函数信息（名称与 JSON 文本参数）。"""

    name: str
    arguments: str = "{}"

    def parse_arguments(self) -> Dict[str, Any]:
        """将 JSON 文本参数解析为字典；解析失败返回空字典。

        Returns:
            参数字典（解析失败按空字典处理，避免中断 Agent 循环）。
        """
        if not self.arguments:
            return {}
        try:
            parsed = json.loads(self.arguments)
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}


@dataclass
class ToolCall:
    """单个工具调用（与 OpenAI tool_calls 元素对应的本地结构）。"""

    id: str
    function: ToolFunction


@dataclass
class LLMResponse:
    """LLM 响应封装（与框架解耦的纯数据结构）。

    Attributes:
        content: 模型生成的文本内容（可能为空，特别是发起工具调用时）。
        tool_calls: 模型请求的工具调用列表（无则为空列表）。
    """

    content: Optional[str] = None
    tool_calls: List[ToolCall] = field(default_factory=list)

    @property
    def has_tool_calls(self) -> bool:
        """是否包含工具调用。"""
        return bool(self.tool_calls)


class LLMClient:
    """OpenAI 兼容 LLM 客户端封装。

    通过 ``AsyncOpenAI`` 发起 chat.completions 调用；openai SDK 仅在真正发起
    调用时才被导入与实例化，因此在无 openai 环境（如纯逻辑单元测试）下导入本
    模块不会报错。
    """

    def __init__(
        self,
        api_key: str,
        api_base: str,
        model_name: str,
        temperature: float,
        timeout_seconds: float,
        tools: Optional[List[Dict[str, Any]]] = None,
        provider_type: str = "openai_compatible",
    ) -> None:
        """初始化 LLM 客户端配置。

        Args:
            api_key: API 密钥（运行时明文，来自加密配置解密）。
            api_base: API 接口地址（兼容 OpenAI 协议，可为空走默认）。
            model_name: 模型名称。
            temperature: 采样温度。
            timeout_seconds: 单次调用超时秒数。
            tools: 提供给模型的工具 schema 列表（function calling，仅 OpenAI 兼容生效）。
            provider_type: 接口类型（openai_compatible / anthropic / gemini /
                dashscope_app）。非 OpenAI 兼容协议走纯对话调用（不含工具）。
        """
        self.api_key = api_key
        self.api_base = api_base
        self.model_name = model_name
        self.temperature = temperature
        self.timeout_seconds = timeout_seconds
        self.tools = tools or []
        self.provider_type = provider_type or "openai_compatible"
        self._client: Any = None

    def _ensure_client(self) -> Any:
        """惰性创建 AsyncOpenAI 客户端（首次发起调用时才导入 openai）。

        Returns:
            AsyncOpenAI 客户端实例。

        Raises:
            ImportError: 未安装 openai 依赖时抛出，由上层捕获回退默认回复。
        """
        if self._client is None:
            base = (self.api_base or "").strip()
            if not base:
                raise RuntimeError("未配置 API 地址，拒绝访问默认云端模型")
            from openai import AsyncOpenAI  # 局部导入仅为惰性加载可选依赖

            self._client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=base,
                timeout=self.timeout_seconds,
            )
        return self._client

    async def aclose(self) -> None:
        """关闭底层 AsyncOpenAI 客户端，释放其持有的 httpx 连接池（避免泄漏）。

        仅 ``openai_compatible`` 分支会惰性创建长生命周期客户端；其它协议分支用
        ``async with httpx.AsyncClient`` 已自动释放，无需在此处理。多次调用安全。
        """
        client = self._client
        self._client = None
        if client is None:
            return
        try:
            close = getattr(client, "close", None)
            if close is not None:
                result = close()
                if asyncio.iscoroutine(result):
                    await result
        except Exception:  # noqa: BLE001 - 关闭失败不影响主流程
            pass

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        tool_choice: str = "auto",
    ) -> LLMResponse:
        """发送聊天请求到 LLM 并返回规整后的响应（按接口类型分流）。

        - openai_compatible：经 AsyncOpenAI，支持工具（function calling）调用；
        - anthropic / gemini / dashscope_app：经 httpx 纯对话调用（不含工具，
          直接返回文本内容），覆盖各服务商官方协议。

        Args:
            messages: OpenAI 风格消息列表。
            tool_choice: 工具选择策略（auto / none，仅 OpenAI 兼容生效）。

        Returns:
            规整后的 LLMResponse（含文本内容与工具调用列表）。
        """
        if self.provider_type == "anthropic":
            return await self._chat_anthropic(messages)
        if self.provider_type == "gemini":
            return await self._chat_gemini(messages)
        if self.provider_type == "dashscope_app":
            return await self._chat_dashscope_app(messages)
        return await self._chat_openai(messages, tool_choice)

    async def _chat_openai(
        self, messages: List[Dict[str, Any]], tool_choice: str
    ) -> LLMResponse:
        """OpenAI 兼容协议调用（支持工具调用）。"""
        client = self._ensure_client()

        request_kwargs: Dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,
            "temperature": self.temperature,
        }
        if self.tools:
            request_kwargs["tools"] = self.tools
            request_kwargs["tool_choice"] = tool_choice

        response = await client.chat.completions.create(**request_kwargs)
        message = response.choices[0].message

        tool_calls: List[ToolCall] = []
        raw_tool_calls = getattr(message, "tool_calls", None) or []
        for tc in raw_tool_calls:
            fn = getattr(tc, "function", None)
            tool_calls.append(
                ToolCall(
                    id=getattr(tc, "id", "") or "",
                    function=ToolFunction(
                        name=getattr(fn, "name", "") or "",
                        arguments=getattr(fn, "arguments", "") or "{}",
                    ),
                )
            )

        return LLMResponse(
            content=getattr(message, "content", None),
            tool_calls=tool_calls,
        )

    @staticmethod
    def _split_system_and_turns(
        messages: List[Dict[str, Any]],
    ) -> tuple[str, List[Dict[str, str]]]:
        """将 OpenAI 风格消息拆分为「系统指令」与「user/assistant 对话轮次」。

        工具消息（role=tool）与含 tool_calls 的 assistant 消息在非 OpenAI 协议
        的纯对话调用中无意义，统一忽略；仅保留纯文本的 user / assistant 轮次。

        Args:
            messages: OpenAI 风格消息列表。

        Returns:
            (系统指令文本, [{"role": "user"/"assistant", "content": str}, ...])。
        """
        system_parts: List[str] = []
        turns: List[Dict[str, str]] = []
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content")
            if role == "system":
                if content:
                    system_parts.append(str(content))
            elif role in ("user", "assistant"):
                # 跳过含工具调用而无文本的 assistant 消息
                if content and not msg.get("tool_calls"):
                    turns.append({"role": role, "content": str(content)})
        return "\n".join(system_parts), turns

    async def _chat_anthropic(self, messages: List[Dict[str, Any]]) -> LLMResponse:
        """Anthropic Claude 协议纯对话调用（/v1/messages）。"""
        import httpx

        system_content, turns = self._split_system_and_turns(messages)
        if not (self.api_base or "").strip():
            raise RuntimeError("未配置 API 地址，拒绝访问默认云端模型")
        base = self.api_base.strip().rstrip("/")
        url = f"{base}/v1/messages" if not base.endswith("/v1") else f"{base}/messages"

        payload: Dict[str, Any] = {
            "model": self.model_name,
            "max_tokens": 1024,
            "temperature": self.temperature,
            "messages": turns or [{"role": "user", "content": ""}],
        }
        if system_content:
            payload["system"] = system_content

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            resp = await client.post(
                url,
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

        # Anthropic 返回 content 为分块列表，拼接其中的 text 块。
        text_parts = [
            block.get("text", "")
            for block in (data.get("content") or [])
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return LLMResponse(content="".join(text_parts).strip() or None)

    async def _chat_gemini(self, messages: List[Dict[str, Any]]) -> LLMResponse:
        """Google Gemini 协议纯对话调用（generateContent）。"""
        import httpx

        system_content, turns = self._split_system_and_turns(messages)
        if not (self.api_base or "").strip():
            raise RuntimeError("未配置 API 地址，拒绝访问默认云端模型")
        base = self.api_base.strip().rstrip("/")
        version_seg = "" if (base.endswith("/v1beta") or base.endswith("/v1")) else "/v1beta"
        url = f"{base}{version_seg}/models/{self.model_name}:generateContent"

        # Gemini 用 contents（role 仅 user/model），assistant 映射为 model。
        contents: List[Dict[str, Any]] = []
        for turn in turns:
            role = "model" if turn["role"] == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": turn["content"]}]})
        if not contents:
            contents = [{"role": "user", "parts": [{"text": ""}]}]

        payload: Dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": self.temperature,
                "maxOutputTokens": 1024,
            },
        }
        if system_content:
            payload["systemInstruction"] = {"parts": [{"text": system_content}]}

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            resp = await client.post(
                url,
                headers={
                    "Content-Type": "application/json",
                    # 经请求头传递密钥，避免出现在 URL 查询串而随日志泄漏。
                    "x-goog-api-key": self.api_key,
                },
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError):
            text = ""
        return LLMResponse(content=(text or "").strip() or None)

    async def _chat_dashscope_app(self, messages: List[Dict[str, Any]]) -> LLMResponse:
        """阿里云百炼 DashScope 应用协议纯对话调用（/apps/{app_id}/completion）。"""
        import httpx

        system_content, turns = self._split_system_and_turns(messages)
        base = (self.api_base or "").strip()
        if "/apps/" not in base:
            raise RuntimeError("DashScope 应用地址中未找到 app_id")
        app_id = base.split("/apps/", 1)[1].split("/", 1)[0]
        url = f"https://dashscope.aliyuncs.com/api/v1/apps/{app_id}/completion"

        # DashScope 应用接收单一 prompt：拼接系统指令与最近一轮用户消息。
        last_user = ""
        for turn in reversed(turns):
            if turn["role"] == "user":
                last_user = turn["content"]
                break
        if system_content and last_user:
            prompt = f"{system_content}\n\n用户问题：{last_user}\n\n请用中文直接回答："
        else:
            prompt = last_user or system_content

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            resp = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "input": {"prompt": prompt},
                    "parameters": {"max_tokens": 1024, "temperature": self.temperature},
                    "debug": {},
                },
            )
            resp.raise_for_status()
            data = resp.json()

        try:
            text = data["output"]["text"]
        except (KeyError, TypeError):
            text = ""
        return LLMResponse(content=(text or "").strip() or None)


__all__ = [
    "ToolFunction",
    "ToolCall",
    "LLMResponse",
    "LLMClient",
]
