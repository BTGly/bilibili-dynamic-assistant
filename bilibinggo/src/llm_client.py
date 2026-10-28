from __future__ import annotations

import atexit
import time
import json
import re
from typing import Any

import httpx

from src.llm_config import LlmConfig, load_llm_config

import threading

_llm_lock = threading.Semaphore(3)
_LLM_TIMEOUT = httpx.Timeout(connect=10.0, read=90.0, write=20.0, pool=5.0)
_LLM_RETRY_ATTEMPTS = 2
_LLM_MAX_TOKENS = 2048
_client_lock = threading.Lock()
_clients: dict[str, httpx.Client] = {}


def close_llm_clients():
    with _client_lock:
        clients = list(_clients.values())
        _clients.clear()
    for client in clients:
        client.close()


atexit.register(close_llm_clients)


def _pooled_client(url):
    # One pool per origin; auth is request scoped, never a shared default header.
    origin = str(httpx.URL(url).copy_with(path="/", query=None))
    with _client_lock:
        client = _clients.get(origin)
        if client is None or client.is_closed:
            client = httpx.Client(timeout=_LLM_TIMEOUT,
                                  limits=httpx.Limits(max_connections=3, max_keepalive_connections=3))
            _clients[origin] = client
        return client


def _format_request_error(exc):
    if isinstance(exc, httpx.ReadTimeout):
        return "LLM 等待模型响应超时；未自动重发请求"
    if isinstance(exc, httpx.ConnectTimeout):
        return "LLM 建立连接超时"
    if isinstance(exc, httpx.PoolTimeout):
        return "LLM 等待连接池超时"
    if isinstance(exc, httpx.WriteTimeout):
        return "LLM 发送请求超时；未自动重发请求"
    return f"无法连接 LLM 接口：{type(exc).__name__}"
# 连接测试只需极短回复，但 DeepSeek V4 等模型在部分网关下 max_tokens=1 会触发 400。
_LLM_TEST_MAX_TOKENS = 64


def _format_llm_http_error(exc: httpx.HTTPStatusError) -> str:
    status = exc.response.status_code
    message = ""
    try:
        body = exc.response.json()
        if isinstance(body.get("error"), dict):
            message = str(body["error"].get("message") or "").strip()
        else:
            message = str(body.get("message") or body.get("detail") or "").strip()
    except Exception:
        message = (exc.response.text or "").strip()[:200]
    if status == 401:
        return f"API Key 无效或已过期{('：' + message) if message else ''}"
    if status == 404:
        return f"接口地址或模型不存在{('：' + message) if message else ''}"
    if status in {408, 429, 500, 502, 503, 504}:
        return f"LLM 服务暂时不可用（HTTP {status}）{('：' + message) if message else ''}"
    return f"连接失败（HTTP {status}）{('：' + message) if message else ''}"


# 这类失败要靠人充值/换 Key，或者只是服务商限流，重试队列不该因此把行判成待核查：
# 不能消耗刷新队列的重试额度
_PROVIDER_BLOCKED_PATTERNS = (
    "API Key 无效或已过期", "接口地址或模型不存在", "未配置 LLM",
    "account balance", "余额不足", "insufficient", "arrearage",
    "LLM 服务暂时不可用", "无法连接 LLM 接口", "rate limiting",
)


def is_provider_blocked(error: BaseException | str) -> bool:
    text = str(error)
    return any(pattern.lower() in text.lower() for pattern in _PROVIDER_BLOCKED_PATTERNS)


def _llm_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.ConnectTimeout, httpx.ConnectError)):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (429, 500, 502, 503, 504)


def _is_zhipu(config: LlmConfig) -> bool:
    base = (config.base_url or "").lower()
    model = (config.model_name or "").lower()
    return "bigmodel" in base or "zhipu" in base or model.startswith("glm")


def _supports_thinking_toggle(config: LlmConfig) -> bool:
    base = (config.base_url or "").lower()
    model = (config.model_name or "").lower()
    # 智谱 GLM-4.5/4.6/4.7 系列与 DeepSeek 一样认 payload 里的 thinking 开关
    return "deepseek" in base or "deepseek" in model or _is_zhipu(config)


def _apply_thinking_off(config: LlmConfig, payload: dict[str, Any]) -> None:
    """结构化抽取不需要链式推理；按网关选择其认识的关闭参数。

    实测硅基流动 Qwen3-8B：开启思考时单次 19.2s / 输出 429 token（其中思考 390），
    关闭后 2.1s / 38 token，抽取结果逐字一致。
    """
    base = (config.base_url or "").lower()
    model = (config.model_name or "").lower()
    if "siliconflow" in base or "qwen3" in model:
        payload["enable_thinking"] = False
        return
    if _supports_thinking_toggle(config):
        payload["thinking"] = {"type": "disabled"}


def _build_chat_payload(
    config: LlmConfig,
    *,
    messages: list[dict[str, str]],
    temperature: float = 0,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": config.model_name,
        "messages": messages,
        "temperature": temperature,
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    _apply_thinking_off(config, payload)
    return payload


def _post_chat_completion(*, url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    client = _pooled_client(url)
    for attempt in range(_LLM_RETRY_ATTEMPTS):
        try:
            resp = client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                raise RuntimeError("LLM 接口返回的 JSON 不是对象")
            return data
        except httpx.HTTPError as exc:
            if attempt + 1 == _LLM_RETRY_ATTEMPTS or not _llm_retryable(exc):
                raise
            delay = 0.5
            if isinstance(exc, httpx.HTTPStatusError):
                try:
                    delay = max(0.0, min(5.0, float(exc.response.headers.get("Retry-After", "0.5"))))
                except ValueError:
                    pass
            time.sleep(delay)
    raise RuntimeError("LLM 重试次数已耗尽")


def test_llm_connection(config: LlmConfig) -> str:
    payload = _build_chat_payload(
        config,
        messages=[{"role": "user", "content": "Reply with exactly: ok"}],
        temperature=0,
        max_tokens=_LLM_TEST_MAX_TOKENS,
    )
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }
    url = f"{config.base_url}/chat/completions"
    try:
        _post_chat_completion(url=url, headers=headers, payload=payload)
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(_format_llm_http_error(exc)) from exc
    except httpx.RequestError as exc:
        raise RuntimeError(_format_request_error(exc)) from exc
    return f"{config.model_name} @ {config.base_url}"


def chat_json(*, system: str, user: str, config: LlmConfig | None = None) -> dict[str, Any]:
    cfg = config or load_llm_config()
    if not cfg:
        raise RuntimeError("未配置 LLM，请检查 config/llm.env")

    payload = _build_chat_payload(
        cfg,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0,
        max_tokens=_LLM_MAX_TOKENS,
    )
    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
    }
    url = f"{cfg.base_url}/chat/completions"

    with _llm_lock:
        try:
            data = _post_chat_completion(url=url, headers=headers, payload=payload)
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(_format_llm_http_error(exc)) from exc
        except httpx.RequestError as exc:
            raise RuntimeError(_format_request_error(exc)) from exc

    choice = (data.get("choices") or [{}])[0]
    if choice.get("finish_reason") == "length":
        raise RuntimeError("LLM 输出达到长度上限，结构化结果不完整，需核查")
    content = ((choice.get("message") or {}).get("content") or "").strip()
    # Parsing the same broken response twice cannot repair it.
    return _extract_json_object(content)


def _extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise RuntimeError(f"LLM 返回无法解析为 JSON: {text[:200]}")
    parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise RuntimeError("LLM 返回的 JSON 不是对象")
    return parsed
