"""Tests for the Kimi-primary / Anthropic-fallback cascade (config#1659).

When the primary ``llm`` spec is non-Anthropic (e.g. an OpenRouter
open-weight model) and its own attempt (first pass + self-heal recovery)
either hard-fails (min_web_searches floor) or silently produces no usable
content — a real, live-verified failure mode for reasoning-capable
OpenRouter models, 2026-07-06 — ``generate_script`` falls through to ONE
fresh attempt on the Anthropic default rather than aborting outright.
Every call also writes a ``{date}-{edition}.llm_decision.json`` recording
which model actually produced (or failed to produce) the script.

Uses a duck-typed fake ``LLMClient`` (real krepis ``GroundedResult``/
``LLMUsage`` dataclasses, no real SDK/network) dispatched by provider, so
each test scripts exactly what the primary vs. fallback calls return.
"""

from __future__ import annotations

import json

import pytest
from krepis.llm import GroundedResult, LLMUsage

from morning_signal import claude


def _grounded(*, provider, model, text, n_searches):
    return GroundedResult(
        text=text, model=model, provider=provider,
        usage=LLMUsage(web_search_requests=n_searches),
        raw_request={}, raw_response=None, searches=[], citations=[],
    )


def _client_factory(plan):
    """``plan`` maps provider -> list of GroundedResult to pop per call
    (so a provider hit twice, e.g. primary first-pass + recovery, can
    return different results in sequence)."""
    remaining = {k: list(v) for k, v in plan.items()}

    class _FakeClient:
        def __init__(self, spec, **kw):
            self.spec = spec

        def complete_grounded(self, **kw):
            queue = remaining.get(self.spec.provider)
            if not queue:
                raise AssertionError(
                    f"no more scripted responses for provider={self.spec.provider!r}"
                )
            return queue.pop(0)

    return _FakeClient


def _base_config(**overrides):
    cfg = {
        "llm": '{"provider": "openrouter", "model": "moonshotai/kimi-k2.6", "reasoning": {"exclude": true}}',
        "claude_model": "claude-haiku-4-5",
        "max_tokens": 4096,
        "web_search_max_uses": 20,
        "min_web_searches": 1,
    }
    cfg.update(overrides)
    return cfg


@pytest.fixture(autouse=True)
def _patched(monkeypatch):
    monkeypatch.setattr(claude, "load_prompt", lambda weekend=False: "SYSTEM PROMPT")
    monkeypatch.setattr(claude, "load_news_context", lambda config, run_date=None: "")
    monkeypatch.setattr(claude, "is_non_trading_day", lambda date_str: False)
    monkeypatch.setattr(claude, "record_result_cost", lambda **kw: 0.0)
    monkeypatch.setattr(claude, "record_search_events",
                        lambda **kw: len(kw["searches"]))
    monkeypatch.setattr(claude, "capture_llm_call", lambda *a, **kw: False)
    monkeypatch.delenv(claude.LLM_ENV_VAR, raising=False)


def _decision_path(tmp_path, date_str="2026-07-06", edition="am"):
    return tmp_path / f"{date_str}-{edition}.llm_decision.json"


def test_falls_back_when_primary_produces_empty_content(monkeypatch, tmp_path):
    monkeypatch.setattr(claude._config, "EPISODES_DIR", tmp_path)
    plan = {
        "openrouter": [
            _grounded(provider="openrouter", model="moonshotai/kimi-k2.6",
                      text="", n_searches=10),
        ],
        "anthropic": [
            _grounded(provider="anthropic", model="claude-haiku-4-5",
                      text="Welcome to Morning Signal. Real content here.",
                      n_searches=5),
        ],
    }
    monkeypatch.setattr(claude, "LLMClient", _client_factory(plan))

    script = claude.generate_script(_base_config(), "2026-07-06", "am")

    assert "Real content here" in script

    decision = json.loads(_decision_path(tmp_path).read_text())
    assert decision["primary_provider"] == "openrouter"
    assert decision["used_provider"] == "anthropic"
    assert decision["fell_back"] is True
    assert decision["primary_outcome"]["script_chars"] == 0
    assert decision["fallback_outcome"]["script_chars"] > 0


def test_falls_back_when_primary_hits_min_web_searches_floor(monkeypatch, tmp_path):
    monkeypatch.setattr(claude._config, "EPISODES_DIR", tmp_path)
    plan = {
        "openrouter": [
            _grounded(provider="openrouter", model="moonshotai/kimi-k2.6",
                      text="some text but too few searches", n_searches=0),
        ],
        "anthropic": [
            _grounded(provider="anthropic", model="claude-haiku-4-5",
                      text="Welcome to Morning Signal. Fallback content.",
                      n_searches=3),
        ],
    }
    monkeypatch.setattr(claude, "LLMClient", _client_factory(plan))

    script = claude.generate_script(_base_config(), "2026-07-06", "am")

    assert "Fallback content" in script
    decision = json.loads(_decision_path(tmp_path).read_text())
    assert decision["fell_back"] is True
    assert decision["primary_outcome"] is None  # the floor raised before an outcome existed


def test_hard_aborts_when_both_primary_and_fallback_fail(monkeypatch, tmp_path):
    monkeypatch.setattr(claude._config, "EPISODES_DIR", tmp_path)
    plan = {
        "openrouter": [
            _grounded(provider="openrouter", model="moonshotai/kimi-k2.6",
                      text="", n_searches=10),
        ],
        "anthropic": [
            _grounded(provider="anthropic", model="claude-haiku-4-5",
                      text="", n_searches=5),
        ],
    }
    monkeypatch.setattr(claude, "LLMClient", _client_factory(plan))

    with pytest.raises(SystemExit):
        claude.generate_script(_base_config(), "2026-07-06", "am")

    # The decision log still records the (failed) attempt — worth knowing
    # both models failed today, not just silence.
    decision = json.loads(_decision_path(tmp_path).read_text())
    assert decision["fell_back"] is True
    assert decision["fallback_outcome"]["script_chars"] == 0


def test_no_fallback_when_primary_is_already_anthropic(monkeypatch, tmp_path):
    """Existing behavior preserved exactly: an anthropic-only config that
    hits the min_web_searches floor hard-aborts immediately, no wasted
    second call to the same model."""
    monkeypatch.setattr(claude._config, "EPISODES_DIR", tmp_path)
    plan = {
        "anthropic": [
            _grounded(provider="anthropic", model="claude-haiku-4-5",
                      text="not enough searching", n_searches=0),
        ],
    }
    monkeypatch.setattr(claude, "LLMClient", _client_factory(plan))

    config = _base_config()
    config.pop("llm")  # legacy anthropic-default resolution path

    with pytest.raises(RuntimeError, match="web_search floor not met"):
        claude.generate_script(config, "2026-07-06", "am")

    # No decision log at all — the exception propagates before we'd write one.
    assert not _decision_path(tmp_path).exists()


def test_no_fallback_when_env_override_pins_exact_spec(monkeypatch, tmp_path):
    """MORNING_SIGNAL_LLM is the operator/test escape hatch: it means run
    EXACTLY this spec, not "with a hidden fallback"."""
    monkeypatch.setattr(claude._config, "EPISODES_DIR", tmp_path)
    monkeypatch.setenv(claude.LLM_ENV_VAR, "openrouter:moonshotai/kimi-k2.6")
    plan = {
        "openrouter": [
            _grounded(provider="openrouter", model="moonshotai/kimi-k2.6",
                      text="", n_searches=10),
        ],
    }
    monkeypatch.setattr(claude, "LLMClient", _client_factory(plan))

    with pytest.raises(SystemExit):
        claude.generate_script(_base_config(), "2026-07-06", "am")


def test_decision_log_written_on_ordinary_success_no_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(claude._config, "EPISODES_DIR", tmp_path)
    plan = {
        "openrouter": [
            _grounded(provider="openrouter", model="moonshotai/kimi-k2.6",
                      text="Welcome to Morning Signal. All good.", n_searches=8),
        ],
    }
    monkeypatch.setattr(claude, "LLMClient", _client_factory(plan))

    script = claude.generate_script(_base_config(), "2026-07-06", "am")

    assert "All good" in script
    decision = json.loads(_decision_path(tmp_path).read_text())
    assert decision["primary_provider"] == "openrouter"
    assert decision["used_provider"] == "openrouter"
    assert decision["fell_back"] is False
