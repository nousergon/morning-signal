"""Unit tests for ``morning_signal.cost_telemetry``.

Locks down the JSONL contract: one line per call, expected keys, and
the cost figure computed against the packaged default rate card.
Uses duck-typed fakes so no real ``anthropic`` client is constructed.
"""

from __future__ import annotations

import json

import pytest

from morning_signal.cost_telemetry import record_call_cost


class _FakeServerToolUsage:
    def __init__(self, *, web_search_requests: int = 0, web_fetch_requests: int = 0):
        self.web_search_requests = web_search_requests
        self.web_fetch_requests = web_fetch_requests


class _FakeUsage:
    def __init__(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_read_input_tokens: int | None = None,
        cache_creation_input_tokens: int | None = None,
        server_tool_use: _FakeServerToolUsage | None = None,
    ):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = cache_read_input_tokens
        self.cache_creation_input_tokens = cache_creation_input_tokens
        self.server_tool_use = server_tool_use


class _FakeMessage:
    def __init__(self, *, model: str, usage: _FakeUsage):
        self.model = model
        self.usage = usage


def test_record_writes_one_jsonl_line(tmp_path):
    msg = _FakeMessage(
        model="claude-sonnet-4-6",
        usage=_FakeUsage(input_tokens=850, output_tokens=2700),
    )
    cost = record_call_cost(
        msg=msg, date_str="2026-05-25", edition="am", episodes_dir=tmp_path,
    )

    out = tmp_path / "2026-05-25-am.cost.jsonl"
    assert out.exists()
    lines = out.read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["date"] == "2026-05-25"
    assert rec["edition"] == "am"
    assert rec["model"] == "claude-sonnet-4-6"
    assert rec["input_tokens"] == 850
    assert rec["output_tokens"] == 2700
    assert rec["web_search_requests"] == 0
    assert rec["cost_usd"] == pytest.approx(cost)


def test_appends_subsequent_calls_to_same_file(tmp_path):
    # Forward-compat: per-segment fanout would produce multiple calls
    # per (date, edition). All append to the same JSONL.
    for _ in range(3):
        msg = _FakeMessage(
            model="claude-haiku-4-5",
            usage=_FakeUsage(input_tokens=100, output_tokens=200),
        )
        record_call_cost(
            msg=msg, date_str="2026-05-25", edition="pm", episodes_dir=tmp_path,
        )

    out = tmp_path / "2026-05-25-pm.cost.jsonl"
    assert len(out.read_text().strip().splitlines()) == 3


def test_includes_web_search_fee_in_cost(tmp_path):
    msg = _FakeMessage(
        model="claude-sonnet-4-6",
        usage=_FakeUsage(
            input_tokens=1_000_000,
            output_tokens=0,
            server_tool_use=_FakeServerToolUsage(web_search_requests=10),
        ),
    )
    cost = record_call_cost(
        msg=msg, date_str="2026-05-25", edition="am", episodes_dir=tmp_path,
    )
    # 1M Sonnet input @ $3/M + 10 web_search @ $10/1k = $3.10.
    assert cost == pytest.approx(3.10)

    rec = json.loads((tmp_path / "2026-05-25-am.cost.jsonl").read_text())
    assert rec["web_search_requests"] == 10
    assert rec["cost_usd"] == pytest.approx(3.10)


def test_creates_episodes_dir_if_absent(tmp_path):
    nested = tmp_path / "fresh-episodes-dir"
    assert not nested.exists()

    msg = _FakeMessage(
        model="claude-sonnet-4-6",
        usage=_FakeUsage(input_tokens=1, output_tokens=1),
    )
    record_call_cost(
        msg=msg, date_str="2026-05-25", edition="am", episodes_dir=nested,
    )
    assert nested.exists()
    assert (nested / "2026-05-25-am.cost.jsonl").exists()
