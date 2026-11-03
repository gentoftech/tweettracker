"""5-Provider LLM Router (Build 1: live Google Gemini & xAI Grok; Build 2 slots)."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any, Protocol

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from tweettracker.crypto import resolve_secret
from tweettracker.models import Workspace
from tweettracker.providers.base import ProviderNotConfiguredError
from tweettracker.providers.spend import enforce_spend_caps, log_api_credit


@dataclass(slots=True)
class LLMResponse:
    provider: str
    model: str
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_usd: float = 0.0


class LLMProvider(Protocol):
    provider_name: str

    async def complete(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1024,
        session: AsyncSession | None = None,
        workspace: Workspace | None = None,
    ) -> LLMResponse: ...


class GeminiProvider:
    """Live Google Gemini REST adapter reading GOOGLE_AI_API_KEY from os.environ."""

    provider_name = "google_gemini"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        timeout_secs: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._explicit_key = api_key
        self.model = (
            model
            or os.environ.get("GEMINI_MODEL", "").strip()
            or "gemini-3.8-flash"
        )
        self.timeout_secs = timeout_secs
        self._client = client

    def _resolve_key(self) -> str:
        key = (
            self._explicit_key
            if self._explicit_key is not None
            else resolve_secret("GOOGLE_AI_API_KEY")
        ).strip()
        if not key:
            raise ProviderNotConfiguredError(
                "GOOGLE_AI_API_KEY is not set in Settings or environment variables."
            )
        return key

    async def complete(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1024,
        session: AsyncSession | None = None,
        workspace: Workspace | None = None,
    ) -> LLMResponse:
        key = self._resolve_key()
        if session is not None and workspace is not None:
            await enforce_spend_caps(session, workspace, projected_usd=0.0005)

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        body: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if system_prompt:
            body["systemInstruction"] = {"parts": [{"text": system_prompt}]}

        headers = {"x-goog-api-key": key, "Content-Type": "application/json"}
        if self._client is not None:
            resp = await self._client.post(url, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        else:
            async with httpx.AsyncClient(timeout=self.timeout_secs) as client:
                resp = await client.post(url, json=body, headers=headers)
                resp.raise_for_status()
                data = resp.json()

        candidates = data.get("candidates") or []
        text = ""
        if candidates and isinstance(candidates[0], dict):
            parts = candidates[0].get("content", {}).get("parts") or []
            text = "".join(str(p.get("text", "")) for p in parts if isinstance(p, dict))

        usage = data.get("usageMetadata") or {}
        in_tok = int(usage.get("promptTokenCount") or 0)
        out_tok = int(usage.get("candidatesTokenCount") or 0)
        est_usd = (in_tok * 0.15 + out_tok * 0.60) / 1_000_000.0

        if session is not None:
            await log_api_credit(
                session,
                workspace_id=workspace.id if workspace else None,
                provider=self.provider_name,
                endpoint=f"gemini:{self.model}",
                units_consumed=float(in_tok + out_tok),
                estimated_usd=est_usd,
            )

        return LLMResponse(
            provider=self.provider_name,
            model=self.model,
            text=text.strip(),
            input_tokens=in_tok,
            output_tokens=out_tok,
            estimated_usd=est_usd,
        )


class GrokProvider:
    """Live xAI Grok REST adapter reading XAI_API_KEY (`https://api.x.ai/v1/chat/completions`)."""

    provider_name = "xai_grok"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        timeout_secs: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._explicit_key = api_key
        self.model = (
            model
            or os.environ.get("XAI_MODEL", "").strip()
            or "grok-4.7"
        )
        self.timeout_secs = timeout_secs
        self._client = client

    def _resolve_key(self) -> str:
        key = (
            self._explicit_key
            if self._explicit_key is not None
            else resolve_secret("XAI_API_KEY")
        ).strip()
        if not key:
            raise ProviderNotConfiguredError(
                "XAI_API_KEY is not set in Settings or environment variables."
            )
        return key

    async def complete(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1024,
        session: AsyncSession | None = None,
        workspace: Workspace | None = None,
    ) -> LLMResponse:
        key = self._resolve_key()
        if session is not None and workspace is not None:
            await enforce_spend_caps(session, workspace, projected_usd=0.001)

        url = "https://api.x.ai/v1/chat/completions"
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        body = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

        if self._client is not None:
            resp = await self._client.post(url, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        else:
            async with httpx.AsyncClient(timeout=self.timeout_secs) as client:
                resp = await client.post(url, json=body, headers=headers)
                resp.raise_for_status()
                data = resp.json()

        choices = data.get("choices") or []
        text = ""
        if choices and isinstance(choices[0], dict):
            text = str(choices[0].get("message", {}).get("content") or "")

        usage = data.get("usage") or {}
        in_tok = int(usage.get("prompt_tokens") or 0)
        out_tok = int(usage.get("completion_tokens") or 0)
        est_usd = (in_tok * 0.30 + out_tok * 0.50) / 1_000_000.0

        if session is not None:
            await log_api_credit(
                session,
                workspace_id=workspace.id if workspace else None,
                provider=self.provider_name,
                endpoint=f"xai:{self.model}",
                units_consumed=float(in_tok + out_tok),
                estimated_usd=est_usd,
            )

        return LLMResponse(
            provider=self.provider_name,
            model=self.model,
            text=text.strip(),
            input_tokens=in_tok,
            output_tokens=out_tok,
            estimated_usd=est_usd,
        )


class StubLLMProvider:
    """Reserved Build-2 slot for openai, anthropic, and deepinfra."""

    def __init__(self, provider_name: str) -> None:
        self.provider_name = provider_name

    async def complete(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1024,
        session: AsyncSession | None = None,
        workspace: Workspace | None = None,
    ) -> LLMResponse:
        raise ProviderNotConfiguredError(
            f"LLMProvider '{self.provider_name}' is a reserved Build 2 config slot "
            f"(per docs/decisions/0002); select 'google_gemini' or 'xai_grok' in Build 1."
        )


def get_llm_provider(provider_name: str) -> LLMProvider:
    normalized = provider_name.strip().lower()
    if normalized == "google_gemini":
        return GeminiProvider()
    if normalized == "xai_grok":
        return GrokProvider()
    if normalized in ("openai", "anthropic", "deepinfra"):
        return StubLLMProvider(normalized)
    raise ProviderNotConfiguredError(f"Unknown LLMProvider '{provider_name}'.")
