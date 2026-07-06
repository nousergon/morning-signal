"""Tests for ``scripts/oss_bakeoff.py`` (config#1659 Phase B shadow canary).

Mirrors ``tests/test_canary.py``'s philosophy: stub the network boundary
(``LLMClient.complete_grounded``) and exercise the parity-comparison logic
and the exit-code matrix without hitting a real Anthropic or OpenRouter
call.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest
from krepis.llm import GroundedResult, LLMUsage
from krepis.llm_config import ModelSpec

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "src"))


@pytest.fixture
def bakeoff_module(monkeypatch, tmp_path: Path):
    """Reload ``oss_bakeoff`` with config/prompt/env wired to tmp paths —
    same shape as ``test_canary.py``'s ``canary_module`` fixture."""
    from morning_signal import config as _config_mod

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "s3_bucket: test-bucket\n"
        "claude_model: claude-haiku-4-5\n"
        "max_tokens: 4096\n"
        "web_search_max_uses: 20\n"
        "min_web_searches: 1\n"
        "required_search_topics:\n"
        "  - name: Political pulse\n"
        "    keywords: [maga]\n"
    )
    (tmp_path / "prompt.md").write_text("Weekday system prompt.\n")
    (tmp_path / "prompt_weekend.md").write_text("Weekend system prompt.\n")

    monkeypatch.setattr(_config_mod, "CONFIG_FILE", cfg_path)
    monkeypatch.setattr(_config_mod, "PROMPT_FILE", tmp_path / "prompt.md")
    monkeypatch.setattr(
        _config_mod, "PROMPT_WEEKEND_FILE", tmp_path / "prompt_weekend.md"
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-abc")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-or-key")
    monkeypatch.delenv("MORNING_SIGNAL_USE_SSM", raising=False)
    monkeypatch.chdir(tmp_path)

    if "oss_bakeoff" in sys.modules:
        del sys.modules["oss_bakeoff"]
    return importlib.import_module("oss_bakeoff")


def _grounded(*, provider, model, unmet_hit, web_search_requests):
    """A GroundedResult whose script/citations satisfy the 'Political
    pulse' topic (keyword 'maga') iff ``unmet_hit`` is False."""
    text = "Welcome to Morning Signal. MAGA reacted today." if not unmet_hit else "Welcome to Morning Signal. Markets only."
    citations = [] if unmet_hit else [{"url": "https://x", "title": "maga rally", "snippet": None}]
    return GroundedResult(
        text=text,
        model=model,
        provider=provider,
        usage=LLMUsage(web_search_requests=web_search_requests),
        raw_request={},
        raw_response=None,
        searches=[],
        citations=citations,
    )


class _FakeClient:
    def __init__(self, spec: ModelSpec, **kw):
        self.spec = spec

    def complete_grounded(self, **kw):
        raise NotImplementedError  # overridden per-test via monkeypatch


def test_run_bakeoff_reports_parity_when_both_cover_topic(bakeoff_module, monkeypatch):
    from morning_signal.config import load_config

    plan = {
        "anthropic": _grounded(provider="anthropic", model="claude-haiku-4-5", unmet_hit=False, web_search_requests=2),
        "openrouter": _grounded(provider="openrouter", model="moonshotai/kimi-k2.6", unmet_hit=False, web_search_requests=2),
    }

    class _Client(_FakeClient):
        def complete_grounded(self, **kw):
            return plan[self.spec.provider]

    monkeypatch.setattr(bakeoff_module, "LLMClient", _Client)

    config = load_config()
    record = bakeoff_module.run_bakeoff(config, "2026-07-06", "am")

    assert record["prod"]["unmet_topics"] == []
    assert record["candidate"]["unmet_topics"] == []
    assert record["parity"]["unmet_topics_match"] is True
    assert record["parity"]["candidate_strictly_worse"] is False
    assert record["parity"]["both_met_min_web_searches"] is True


def test_run_bakeoff_flags_candidate_strictly_worse(bakeoff_module, monkeypatch):
    from morning_signal.config import load_config

    plan = {
        "anthropic": _grounded(provider="anthropic", model="claude-haiku-4-5", unmet_hit=False, web_search_requests=2),
        "openrouter": _grounded(provider="openrouter", model="moonshotai/kimi-k2.6", unmet_hit=True, web_search_requests=2),
    }

    class _Client(_FakeClient):
        def complete_grounded(self, **kw):
            return plan[self.spec.provider]

    monkeypatch.setattr(bakeoff_module, "LLMClient", _Client)

    config = load_config()
    record = bakeoff_module.run_bakeoff(config, "2026-07-06", "am")

    assert record["prod"]["unmet_topics"] == []
    assert record["candidate"]["unmet_topics"] == ["Political pulse"]
    assert record["parity"]["unmet_topics_match"] is False
    assert record["parity"]["candidate_strictly_worse"] is True


def test_main_fails_without_openrouter_key(bakeoff_module, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["oss_bakeoff.py", "--date", "2026-07-06"])
    assert bakeoff_module.main() == 1


def test_main_writes_jsonl_on_success(bakeoff_module, monkeypatch, tmp_path):
    plan = {
        "anthropic": _grounded(provider="anthropic", model="claude-haiku-4-5", unmet_hit=False, web_search_requests=2),
        "openrouter": _grounded(provider="openrouter", model="moonshotai/kimi-k2.6", unmet_hit=False, web_search_requests=2),
    }

    class _Client(_FakeClient):
        def complete_grounded(self, **kw):
            return plan[self.spec.provider]

    monkeypatch.setattr(bakeoff_module, "LLMClient", _Client)
    log_dir = tmp_path / "bakeoff_out"
    monkeypatch.setenv(bakeoff_module.BAKEOFF_LOG_DIR_ENV, str(log_dir))
    monkeypatch.setattr(sys, "argv", ["oss_bakeoff.py", "--date", "2026-07-06", "--edition", "am"])

    assert bakeoff_module.main() == 0

    out_path = log_dir / "2026-07-06-am.bakeoff.jsonl"
    assert out_path.exists()
    record = json.loads(out_path.read_text().strip().splitlines()[0])
    assert record["date"] == "2026-07-06"
    assert record["edition"] == "am"
    assert record["parity"]["unmet_topics_match"] is True


def test_main_appends_on_repeated_runs(bakeoff_module, monkeypatch, tmp_path):
    plan = {
        "anthropic": _grounded(provider="anthropic", model="claude-haiku-4-5", unmet_hit=False, web_search_requests=2),
        "openrouter": _grounded(provider="openrouter", model="moonshotai/kimi-k2.6", unmet_hit=False, web_search_requests=2),
    }

    class _Client(_FakeClient):
        def complete_grounded(self, **kw):
            return plan[self.spec.provider]

    monkeypatch.setattr(bakeoff_module, "LLMClient", _Client)
    log_dir = tmp_path / "bakeoff_out"
    monkeypatch.setenv(bakeoff_module.BAKEOFF_LOG_DIR_ENV, str(log_dir))
    monkeypatch.setattr(sys, "argv", ["oss_bakeoff.py", "--date", "2026-07-06", "--edition", "am"])

    assert bakeoff_module.main() == 0
    assert bakeoff_module.main() == 0

    out_path = log_dir / "2026-07-06-am.bakeoff.jsonl"
    assert len(out_path.read_text().strip().splitlines()) == 2
