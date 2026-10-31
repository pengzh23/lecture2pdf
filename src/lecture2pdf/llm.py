"""OpenAI 兼容接口的薄封装：文本 / 图文调用、重试、并发、用量统计。"""

from __future__ import annotations

import base64
import logging
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TypeVar

from .config import LLMEndpoint

logger = logging.getLogger(__name__)
T = TypeVar("T")
R = TypeVar("R")


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, ep: LLMEndpoint, name: str):
        if not ep.available:
            raise LLMError(f"{name} 模型未配置 API Key")
        if not ep.model:
            raise LLMError(f"{name} 模型未配置模型名")
        import openai

        kw = {"api_key": ep.api_key, "timeout": 300, "max_retries": 0}
        if ep.base_url:
            kw["base_url"] = ep.base_url
        self.client = openai.OpenAI(**kw)
        self.model = ep.model
        self.name = name
        # chat = /v1/chat/completions；responses = /v1/responses（部分中转站 / Codex 渠道只提供后者）
        self.api = "chat"
        self._lock = threading.Lock()
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.calls = 0

    def chat(
        self,
        system: str,
        user: str,
        images: Iterable[Path] = (),
        temperature: float = 0.2,
        max_tokens: int = 8192,
        retries: int = 4,
    ) -> str:
        """输出因长度上限被截断时（推理模型的思考过程也计入上限），自动放宽上限重试，最多到 32768。"""
        images = list(images)
        text, cut = self._chat_retry(system, user, images, temperature, max_tokens, retries)
        while cut and max_tokens < 32768:
            max_tokens *= 2
            logger.info(f"[{self.name}] 输出被截断，放宽长度上限到 {max_tokens} 后重试")
            try:
                text, cut = self._chat_retry(system, user, images, temperature, max_tokens, retries)
            except LLMError as e:  # 服务端不接受更大的上限：保留已有结果
                logger.warning(f"[{self.name}] 放宽长度上限失败（{e}）")
                break
        if not text:
            raise LLMError(f"[{self.name}] 模型返回空内容（输出长度上限内只产生了思考过程）")
        if cut:
            logger.warning(f"[{self.name}] 输出因长度上限被截断")
        return text

    def _chat_retry(self, system, user, images, temperature, max_tokens, retries) -> tuple[str, bool]:
        content: list[dict] | str
        if images:
            content = [{"type": "text", "text": user}]
            for p in images:
                b64 = base64.b64encode(Path(p).read_bytes()).decode()
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "high"},
                })
        else:
            content = user
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": content}]

        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                if self.api == "responses":
                    text, pin, pout, cut = self._responses(system, user, images, max_tokens)
                else:
                    text, pin, pout, cut = self._chat(messages, temperature, max_tokens)
                with self._lock:
                    self.calls += 1
                    self.prompt_tokens += pin
                    self.completion_tokens += pout
                if not text and not cut:  # 截断导致的空内容交给 chat() 放宽上限重试
                    raise LLMError("模型返回空内容")
                return text, cut
            except Exception as e:  # noqa: BLE001  网络/限流/5xx 统一重试
                last = e
                if _endpoint_unsupported(e):  # 并发时可能已被其他线程切换，这里直接改用 responses 重试
                    with self._lock:
                        if self.api == "chat":
                            self.api = "responses"
                            logger.info(f"[{self.name}] 接口不支持 chat/completions，改用 /v1/responses")
                    try:
                        text, pin, pout, cut = self._responses(system, user, images, max_tokens)
                        with self._lock:
                            self.calls += 1
                            self.prompt_tokens += pin
                            self.completion_tokens += pout
                        if text:
                            return text, cut
                    except Exception as e2:  # noqa: BLE001
                        last = e2
                if _is_fatal(last) or attempt == retries:
                    break
                wait = min(60, 3 * 2 ** attempt)
                logger.warning(f"[{self.name}] 调用失败（{e}），{wait}s 后重试")
                time.sleep(wait)
        raise LLMError(f"[{self.name}] 调用失败: {last}")

    def _chat(self, messages, temperature, max_tokens):
        resp = self.client.chat.completions.create(
            model=self.model, messages=messages, temperature=temperature, max_tokens=max_tokens)
        u = getattr(resp, "usage", None)
        return ((resp.choices[0].message.content or "").strip(),
                (u.prompt_tokens or 0) if u else 0, (u.completion_tokens or 0) if u else 0,
                resp.choices[0].finish_reason == "length")

    def _responses(self, system, user, images, max_tokens):
        content: list[dict] = [{"type": "input_text", "text": user}]
        for p in images:
            b64 = base64.b64encode(Path(p).read_bytes()).decode()
            content.append({"type": "input_image", "detail": "high",
                            "image_url": f"data:image/jpeg;base64,{b64}"})
        # 推理模型不接受 temperature；输出上限含推理 token，留出余量
        resp = self.client.responses.create(
            model=self.model, instructions=system,
            input=[{"role": "user", "content": content}],
            max_output_tokens=max_tokens + 16000)
        u = getattr(resp, "usage", None)
        return ((resp.output_text or "").strip(),
                (u.input_tokens or 0) if u else 0, (u.output_tokens or 0) if u else 0,
                getattr(resp, "status", "") == "incomplete")

    def usage_line(self) -> str:
        return (f"{self.name}: {self.calls} 次调用, 输入 {self.prompt_tokens} tokens, "
                f"输出 {self.completion_tokens} tokens（模型 {self.model}）")


def _is_fatal(e: Exception) -> bool:
    """鉴权失败、参数错误这类重试也没用的错误。"""
    status = getattr(e, "status_code", None)
    return status in (400, 401, 403, 404, 422)


def _endpoint_unsupported(e: Exception) -> bool:
    """服务端表示该模型不提供 chat/completions 接口（各中转站说法不一）。"""
    msg = str(e).lower()
    return ("channel_unavailable" in msg or "does not provide this model and endpoint" in msg
            or "not supported in the v1/chat/completions" in msg
            or "use the responses api" in msg
            or (getattr(e, "status_code", None) == 404 and "model" not in msg))


def parallel_map(fn: Callable[[T], R], items: list[T], workers: int) -> list[R]:
    if workers <= 1 or len(items) <= 1:
        return [fn(x) for x in items]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(fn, items))
