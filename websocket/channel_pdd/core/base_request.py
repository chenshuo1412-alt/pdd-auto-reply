# -*- coding: utf-8 -*-
"""
channel_pdd.core.base_request —— 拼多多基础请求层（统一请求 / 重试 / 重登 / 签名检测）
=====================================================================================
本文件用途：复用改造参照项目 Customer-Agent-1.2.0 的
``Channel/pinduoduo/utils/base_request.py``（class BaseRequest），为本系统
websocket 服务的拼多多 HTTP 接口提供统一的请求基类，满足需求 26.1 / 26.2：

核心能力：
1. 统一请求（get / post）：合并默认请求头、携带店铺 Cookie、统一响应解析。
2. 统一重试：网络异常 / 5xx / 429 等可重试错误按「指数退避 + 随机抖动」重试。
3. 会话过期自动重登：当响应 ``error_code == 43001`` 且 ``error_msg`` 含「会话已过期」
   时，自动刷新 / 重新登录并更新 Cookie 后重试原请求（仅尝试一次重登，避免死循环）。
4. anti-content 签名缺失检测（需求 26.1 / 26.2）：对依赖签名的接口，
   - 请求前检测当前 Cookie 是否携带有效签名，缺失则抛出
     ``AntiContentMissingError``（携带中文原因）；
   - 请求后检测响应是否为「签名校验失败 / 风控拦截」，命中则同样抛出领域异常，
     供上层转换为统一响应体（success=false）或记录系统日志，且不中断其它流程。

与参照项目的差异（按本系统架构改造）：
- 凭据读写经 common 公共库（``credential_store``）：Cookie / 密码以可逆加密存储，
  解密后供请求携带（需求 3.6 / 8.6），而非参照项目的明文 SQLite。
- 自动重登经本系统 ``pdd_login`` 模块（后续任务 10.2 实现）；本层以「可注入的
  重登回调」解耦，登录模块缺位时安全降级（仅记录日志，不崩溃）。会话过期检测与
  自动重登逻辑拆分至 ``session_relogin.SessionReloginMixin``（满足规范 35 单文件
  ≤500 行）。
- 日志使用标准库 logging，级别 info/warning/error（规范 38：禁用 debug）。

实现约束（开发规范）：单文件 ≤500 行、文件名用下划线、导入置顶、注释完善、全中文。
"""
from __future__ import annotations

import json
import logging
import random
import time
from typing import Any, Callable, Dict, Optional, Union

import requests

from channel_pdd.core.anti_content import (
    AntiContentMissingError,
    has_valid_anti_content,
    is_signature_invalid_response,
)
from channel_pdd.core.credential_store import load_account_cookies
from common.core.platform_guard import PlatformDenied, enforce, rewrite_platform_http
from channel_pdd.core.session_relogin import (
    SESSION_EXPIRED_ERROR_CODE,
    SESSION_EXPIRED_KEYWORD,
    SessionReloginMixin,
)

logger = logging.getLogger("channel_pdd.base_request")

# 重试随机抖动范围（随机因子乘数），避免「雷鸣群体效应」。
RETRY_JITTER_MIN: float = 0.1
RETRY_JITTER_MAX: float = 0.3

# 默认请求超时（秒）。
DEFAULT_TIMEOUT: int = 30

# 触发重试的 HTTP 状态码（除 5xx 外的可重试客户端 / 网关错误）。
_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({408, 429, 502, 503, 504})


class TransportUnclear(RuntimeError):
    """发送结果不明（超时、断连、5xx 或空响应）。禁止自动重试。"""


class BaseRequest(SessionReloginMixin):
    """拼多多 API 请求基类，统一管理 requests 请求。

    功能特性：
    - 统一请求重试（指数退避 + 随机抖动）；
    - 会话过期（error_code=43001）自动刷新 / 重登并重试（见 SessionReloginMixin）；
    - anti-content 签名缺失 / 失效检测（需求 26.1 / 26.2）；
    - 统一错误处理与日志记录（敏感字段脱敏）。

    自动重新登录说明：当 API 响应包含 ``error_code=43001`` 且 ``error_msg`` 含
    「会话已过期」时，会调用重登回调（默认经本系统 pdd_login 模块）刷新 / 重登
    并更新 Cookie，然后重试原请求。
    """

    def __init__(
        self,
        shop_id: Optional[str] = None,
        user_id: Optional[int] = None,
        channel_name: str = "pinduoduo",
        max_retries: int = 3,
        retry_delay: float = 1.0,
        retry_backoff: float = 2.0,
    ) -> None:
        """初始化请求基类。

        Args:
            shop_id: 拼多多店铺业务标识。
            user_id: 归属用户 ID。
            channel_name: 渠道名称（默认 pinduoduo）。
            max_retries: 最大重试次数（不含首次执行）。
            retry_delay: 初始重试延迟（秒）。
            retry_backoff: 重试退避倍数。
        """
        self.shop_id = shop_id
        self.user_id = user_id
        self.channel_name = channel_name

        # 重试配置。
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.retry_backoff = retry_backoff

        # 默认请求头（模拟浏览器，拼多多接口对 UA / 语言等有校验）。
        self.default_headers: Dict[str, str] = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
            ),
            "Content-Type": "application/json",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
            "priority": "u=1, i",
        }

        # 当前 Cookie 字典（解密后从数据库加载）。
        self.cookies: Dict[str, Any] = {}
        if shop_id is not None and user_id is not None:
            self._init_account_info()

    # ------------------------------------------------------------------
    # 账号信息初始化与 Cookie 加载
    # ------------------------------------------------------------------
    def _init_account_info(self) -> None:
        """从数据库加载并解密当前店铺账号的 Cookie 凭据（需求 3.6）。"""
        try:
            self.cookies = load_account_cookies(
                self.shop_id, self.user_id, self.channel_name
            )
            if not self.cookies:
                logger.warning(
                    "店铺 shop_id=%s 的 Cookie 为空或加载失败", self.shop_id
                )
        except Exception as exc:  # noqa: BLE001 - 加载失败不应中断构造
            logger.error("初始化账户 Cookie 失败: %s", exc)
            self.cookies = {}

    # ------------------------------------------------------------------
    # anti-content 签名缺失检测（需求 26.1 / 26.2）
    # ------------------------------------------------------------------
    def ensure_anti_content(self) -> None:
        """校验当前 Cookie 是否携带有效 anti-content 签名，缺失则抛领域异常。

        供依赖签名的接口（商品列表、商品详情、商品卡片发送）在请求前调用。

        Raises:
            AntiContentMissingError: 当前 Cookie 缺少有效签名（携带中文原因）。
        """
        if not has_valid_anti_content(self.cookies):
            logger.warning("店铺 shop_id=%s 缺少有效 anti-content 签名", self.shop_id)
            raise AntiContentMissingError()

    # ------------------------------------------------------------------
    # 会话过期检测与自动重登：见 channel_pdd.core.session_relogin.SessionReloginMixin
    # （_is_session_expired / set_relogin_func / _relogin_and_update_cookies 等）
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 重试与响应处理
    # ------------------------------------------------------------------
    def _should_retry(
        self,
        response: Optional[requests.Response] = None,
        exception: Optional[Exception] = None,
    ) -> bool:
        """判断是否应重试（网络异常 / 5xx / 特定可重试状态码）。

        Args:
            response: HTTP 响应对象。
            exception: 捕获到的异常。

        Returns:
            应重试返回 True；否则 False。
        """
        if exception is not None:
            return isinstance(
                exception,
                (
                    requests.ConnectionError,
                    requests.Timeout,
                    requests.HTTPError,
                    requests.TooManyRedirects,
                ),
            )
        if response is not None:
            if response.status_code >= 500:
                return True
            if response.status_code in _RETRYABLE_STATUS_CODES:
                return True
        return False

    def _calculate_retry_delay(self, attempt: int) -> float:
        """计算重试延迟（指数退避 + 随机抖动）。

        Args:
            attempt: 当前重试序号（从 0 起）。

        Returns:
            延迟秒数。
        """
        delay = self.retry_delay * (self.retry_backoff ** attempt)
        jitter = random.uniform(RETRY_JITTER_MIN, RETRY_JITTER_MAX) * delay
        return delay + jitter

    def _handle_response(
        self, response: requests.Response, expect_json: bool = True
    ) -> Optional[Dict[str, Any]]:
        """统一处理响应：校验状态码并解析 JSON。

        Args:
            response: requests 响应对象。
            expect_json: 是否期望 JSON 响应。

        Returns:
            解析后的响应字典；失败返回 None。
        """
        try:
            if response.status_code != 200:
                logger.error("请求失败，状态码: %s", response.status_code)
                return None
            if expect_json:
                try:
                    return response.json()
                except json.JSONDecodeError:
                    logger.error("解析 JSON 响应失败")
                    return None
            return {"text": response.text, "status_code": response.status_code}
        except Exception as exc:  # noqa: BLE001
            logger.error("处理响应时发生错误: %s", exc)
            return None

    def _execute_with_retry(
        self,
        request_func: Callable[[], requests.Response],
        expect_json: bool = True,
        check_signature: bool = False,
        max_retries: Optional[int] = None,
        allow_relogin: bool = True,
        transport_raises: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """带重试机制执行请求，并在需要时做签名失效检测与会话过期自动重登。

        Args:
            request_func: 实际发起请求的可调用对象。
            expect_json: 是否期望 JSON 响应。
            check_signature: 是否对响应做 anti-content 签名失效检测（需求 26.2）。

        Returns:
            响应字典；最终失败返回 None。

        Raises:
            AntiContentMissingError: ``check_signature`` 为真且响应判定为签名失效。
        """
        relogin_attempted = False  # 仅允许一次自动重登，避免死循环。
        retries = self.max_retries if max_retries is None else max_retries

        for attempt in range(retries + 1):
            try:
                response = request_func()

                if response is not None and response.status_code == 200:
                    response_data = self._handle_response(response, expect_json)

                    # 会话过期：自动重登后继续重试（仅一次）。
                    if (
                        allow_relogin
                        and response_data
                        and self._is_session_expired(response_data)
                        and not relogin_attempted
                        and self.shop_id is not None
                        and self.user_id is not None
                    ):
                        relogin_attempted = True
                        if self._relogin_and_update_cookies():
                            logger.info("重登成功，重试原请求")
                            continue
                        logger.error("重登失败，请求终止")
                        return response_data

                    # 签名失效检测（需求 26.2）：命中则抛领域异常供上层降级。
                    if check_signature and is_signature_invalid_response(response_data):
                        logger.warning(
                            "店铺 shop_id=%s 接口返回签名校验失败 / 风控拦截",
                            self.shop_id,
                        )
                        raise AntiContentMissingError()

                    return response_data

                # 非 200：按需重试。
                if transport_raises and response is not None and response.status_code >= 500:
                    raise TransportUnclear(f"http {response.status_code}")
                if attempt < retries and self._should_retry(response=response):
                    delay = self._calculate_retry_delay(attempt)
                    logger.warning(
                        "请求失败（状态码 %s），第 %s 次重试，延迟 %.2f 秒",
                        getattr(response, "status_code", "?"),
                        attempt + 1,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                return self._handle_response(response, expect_json) if response else None

            except (AntiContentMissingError, PlatformDenied, TransportUnclear):
                raise
            except Exception as exc:  # noqa: BLE001
                if transport_raises and attempt >= retries:
                    raise TransportUnclear(str(exc)) from exc
                if attempt < retries and self._should_retry(exception=exc):
                    delay = self._calculate_retry_delay(attempt)
                    logger.warning(
                        "请求异常（%s），第 %s 次重试，延迟 %.2f 秒",
                        exc,
                        attempt + 1,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                logger.error("请求最终失败: %s", exc)
                return None

        logger.error("重试 %s 次后仍失败", retries)
        if transport_raises:
            raise TransportUnclear("重试耗尽且结果不明")
        return None

    # ------------------------------------------------------------------
    # 请求方法（get / post）
    # ------------------------------------------------------------------
    def _merge_headers(self, headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """合并默认请求头与自定义请求头。

        Args:
            headers: 自定义请求头。

        Returns:
            合并后的请求头字典。
        """
        merged = self.default_headers.copy()
        if headers:
            merged.update(headers)
        return merged

    def get(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: int = DEFAULT_TIMEOUT,
        expect_json: bool = True,
        check_signature: bool = False,
        purpose: str = "read",
        customer_uid: Optional[str] = None,
        max_retries: Optional[int] = None,
        allow_relogin: bool = True,
        transport_raises: bool = False,
        **kwargs: Any,
    ) -> Optional[Dict[str, Any]]:
        """发起 GET 请求（统一重试 / 重登 / 可选签名检测）。

        Args:
            url: 请求 URL。
            params: URL 查询参数。
            headers: 自定义请求头。
            timeout: 超时秒数。
            expect_json: 是否期望 JSON 响应。
            check_signature: 是否为依赖签名的接口（请求前校验 + 响应失效检测）。
            **kwargs: 透传给 requests 的其它参数。

        Returns:
            响应字典；失败返回 None。

        Raises:
            AntiContentMissingError: 依赖签名接口缺少有效签名或响应判定为签名失效。
        """
        if check_signature:
            self.ensure_anti_content()
        url = rewrite_platform_http(url)
        enforce(purpose, shop_id=self.shop_id, url=url, customer_uid=customer_uid)
        merged_headers = self._merge_headers(headers)
        kwargs["allow_redirects"] = False

        def _make_request() -> requests.Response:
            return requests.get(
                url,
                params=params,
                headers=merged_headers,
                cookies=self.cookies,
                timeout=timeout,
                **kwargs,
            )

        return self._execute_with_retry(
            _make_request,
            expect_json=expect_json,
            check_signature=check_signature,
            max_retries=max_retries,
            allow_relogin=allow_relogin,
            transport_raises=transport_raises,
        )

    def post(
        self,
        url: str,
        data: Optional[Union[Dict[str, Any], str]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: int = DEFAULT_TIMEOUT,
        expect_json: bool = True,
        check_signature: bool = False,
        purpose: str = "read",
        customer_uid: Optional[str] = None,
        max_retries: Optional[int] = None,
        allow_relogin: bool = True,
        transport_raises: bool = False,
        **kwargs: Any,
    ) -> Optional[Dict[str, Any]]:
        """发起 POST 请求（统一重试 / 重登 / 可选签名检测）。

        Args:
            url: 请求 URL。
            data: 表单数据。
            json_data: JSON 请求体。
            headers: 自定义请求头。
            timeout: 超时秒数。
            expect_json: 是否期望 JSON 响应。
            check_signature: 是否为依赖签名的接口（请求前校验 + 响应失效检测）。
            **kwargs: 透传给 requests 的其它参数。

        Returns:
            响应字典；失败返回 None。

        Raises:
            AntiContentMissingError: 依赖签名接口缺少有效签名或响应判定为签名失效。
        """
        if check_signature:
            self.ensure_anti_content()
        url = rewrite_platform_http(url)
        enforce(purpose, shop_id=self.shop_id, url=url, customer_uid=customer_uid)
        merged_headers = self._merge_headers(headers)
        kwargs["allow_redirects"] = False

        def _make_request() -> requests.Response:
            return requests.post(
                url,
                data=data,
                json=json_data,
                headers=merged_headers,
                cookies=self.cookies,
                timeout=timeout,
                **kwargs,
            )

        return self._execute_with_retry(
            _make_request,
            expect_json=expect_json,
            check_signature=check_signature,
            max_retries=max_retries,
            allow_relogin=allow_relogin,
            transport_raises=transport_raises,
        )

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------
    def generate_request_id(self) -> int:
        """生成请求 ID（毫秒时间戳）。

        Returns:
            毫秒级整数请求 ID。
        """
        return int(time.time() * 1000)

    def update_cookies(self, new_cookies: Union[Dict[str, Any], str]) -> None:
        """更新当前实例的 Cookie（不落库）。

        Args:
            new_cookies: 新 Cookie（字典或 JSON 字符串）。
        """
        if isinstance(new_cookies, str):
            try:
                parsed = json.loads(new_cookies)
                self.cookies = parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                logger.error("更新 Cookie 失败: JSON 解析错误")
        elif isinstance(new_cookies, dict):
            self.cookies = new_cookies
        else:
            logger.error("更新 Cookie 失败: 不支持的数据类型 %s", type(new_cookies))

    def set_retry_config(
        self,
        max_retries: Optional[int] = None,
        retry_delay: Optional[float] = None,
        retry_backoff: Optional[float] = None,
    ) -> None:
        """动态调整重试配置。

        Args:
            max_retries: 最大重试次数。
            retry_delay: 初始重试延迟（秒）。
            retry_backoff: 重试退避倍数。
        """
        if max_retries is not None:
            self.max_retries = max_retries
        if retry_delay is not None:
            self.retry_delay = retry_delay
        if retry_backoff is not None:
            self.retry_backoff = retry_backoff


__all__ = [
    "BaseRequest",
    "TransportUnclear",
    "SESSION_EXPIRED_ERROR_CODE",
    "SESSION_EXPIRED_KEYWORD",
]
