"""The real LLM wrapper, driven by a stubbed Anthropic client built from real SDK types."""
import os

import pytest
from anthropic.types import Message, ServerToolUseBlock, ToolUseBlock, Usage, WebSearchResultBlock, WebSearchToolResultBlock

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")


def msg(content, stop="tool_use"):
    return Message(id="m", type="message", role="assistant", model="x", content=content,
                   stop_reason=stop, stop_sequence=None,
                   usage=Usage(input_tokens=10, output_tokens=5))


class Stub:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.replies.pop(0)


@pytest.fixture
def llm(monkeypatch):
    from app.config import settings
    from app.pipeline import llm as mod

    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    return mod.LLM(max_calls=5)


def test_structured_returns_tool_input(llm):
    llm.client = Stub([msg([ToolUseBlock(type="tool_use", id="t1", name="curate_results", input={"results": []})])])
    out = llm.structured(model="m", system="s", payload={"a": 1}, tool_name="curate_results", schema={"type": "object"})
    assert out == {"results": []}
    assert llm.client.calls[0]["tool_choice"] == {"type": "tool", "name": "curate_results"}
    assert llm.usage["calls"] == 1 and llm.usage["input_tokens"] == 10


def test_search_and_report_keeps_only_seen_urls(llm):
    search = WebSearchToolResultBlock(type="web_search_tool_result", tool_use_id="s1", content=[
        WebSearchResultBlock(type="web_search_result", url="https://real.example/a?utm_source=x", title="A",
                             encrypted_content="e", page_age=None)])
    llm.client = Stub([
        msg([ServerToolUseBlock(type="server_tool_use", id="s1", name="web_search", input={"query": "q"}), search],
            stop="pause_turn"),
        msg([ToolUseBlock(type="tool_use", id="t2", name="report_findings", input={"items": [
            {"url": "https://real.example/a", "title": "A", "source_name": "Real"},
            {"url": "https://made-up.example/b", "title": "B", "source_name": "Fake"}]})]),
    ])
    report, seen = llm.search_and_report(model="m", system="s", prompt="p", schema={}, max_searches=3)
    assert "https://real.example/a" in seen
    assert len(report["items"]) == 2  # filtering happens in collect.discover
    assert len(llm.client.calls) == 2, "pause_turn should continue the loop"


def test_budget_cap(llm):
    from app.pipeline.llm import BudgetExceeded

    llm.max_calls = 0
    with pytest.raises(BudgetExceeded):
        llm.structured(model="m", system="s", payload={}, tool_name="x", schema={})
