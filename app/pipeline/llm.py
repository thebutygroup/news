"""Thin wrapper over the Anthropic API.

Two call shapes:
  structured()        one forced tool call that returns JSON matching a schema
  search_and_report() lets the model use the web search server tool, then report findings
                      through a custom tool, so output is always structured

Everything the pipeline knows about the LLM provider lives here. If Bauer mandates a
different provider, implement the same two methods and swap get_llm().
"""
from __future__ import annotations

import json
import logging
from typing import Any

from ..config import settings
from ..normalize import canonical_url

log = logging.getLogger("news.llm")


class BudgetExceeded(RuntimeError):
    pass


class LLM:
    def __init__(self, max_calls: int | None = None):
        import anthropic

        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set. Set it, or set LLM_FAKE=1 for a dry run.")
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=3, timeout=600)
        self.max_calls = max_calls or settings.max_llm_calls_per_run
        self.usage: dict[str, int] = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "web_searches": 0}

    # -- bookkeeping -------------------------------------------------------
    def _spend(self) -> None:
        if self.usage["calls"] >= self.max_calls:
            raise BudgetExceeded(f"LLM call cap of {self.max_calls} per run reached")
        self.usage["calls"] += 1

    def _track(self, resp: Any) -> None:
        u = getattr(resp, "usage", None)
        if u is None:
            return
        self.usage["input_tokens"] += getattr(u, "input_tokens", 0) or 0
        self.usage["output_tokens"] += getattr(u, "output_tokens", 0) or 0
        stu = getattr(u, "server_tool_use", None)
        if stu is not None:
            self.usage["web_searches"] += getattr(stu, "web_search_requests", 0) or 0

    # -- call shapes -------------------------------------------------------
    def structured(self, *, model: str, system: str, payload: dict, tool_name: str, schema: dict,
                   max_tokens: int = 16000) -> dict:
        self._spend()
        resp = self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": json.dumps(payload, default=str)}],
            tools=[{"name": tool_name, "description": "Return your result.", "input_schema": schema}],
            tool_choice={"type": "tool", "name": tool_name},
        )
        self._track(resp)
        for block in resp.content:
            if block.type == "tool_use" and block.name == tool_name:
                return block.input
        raise RuntimeError(f"Model did not return {tool_name} (stop_reason={resp.stop_reason})")

    def search_and_report(self, *, model: str, system: str, prompt: str, schema: dict,
                          max_searches: int) -> tuple[dict, set[str]]:
        """Returns (report, canonical URLs that actually appeared in search results)."""
        tools = [
            {"type": settings.web_search_tool, "name": "web_search", "max_uses": max_searches},
            {"name": "report_findings", "description": "Report everything you found. Call once, at the end.",
             "input_schema": schema},
        ]
        messages: list[dict] = [{"role": "user", "content": prompt}]
        seen_urls: set[str] = set()
        for _ in range(6):
            self._spend()
            resp = self.client.messages.create(model=model, max_tokens=16000, system=system,
                                               messages=messages, tools=tools)
            self._track(resp)
            for block in resp.content:
                if block.type == "web_search_tool_result" and isinstance(block.content, list):
                    for result in block.content:
                        url = getattr(result, "url", None)
                        if url:
                            seen_urls.add(canonical_url(url))
                if block.type == "tool_use" and block.name == "report_findings":
                    return block.input, seen_urls
            messages.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason == "pause_turn":
                continue  # server tool loop hit its turn limit; let it carry on
            messages.append({"role": "user", "content": "Now call report_findings with everything you found."})
        log.warning("Discovery ended without a report")
        return {"items": []}, seen_urls


def get_llm():
    if settings.llm_fake:
        from .fake_llm import FakeLLM

        return FakeLLM()
    return LLM()
