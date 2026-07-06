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


def _dispatcher(plan_by_provider_or_model):
    """A LLMClient fake dispatching by spec.provider (prod, "anthropic")
    or spec.model (each OpenRouter candidate has a distinct model id, so
    provider alone can't disambiguate between them)."""

    class _Client:
        def __init__(self, spec: ModelSpec, **kw):
            self.spec = spec

        def complete_grounded(self, **kw):
            key = self.spec.model if self.spec.provider == "openrouter" else self.spec.provider
            return plan_by_provider_or_model[key]

    return _Client


def _all_pass_plan(*, prod_unmet=False):
    return {
        "anthropic": _grounded(
            provider="anthropic", model="claude-haiku-4-5",
            unmet_hit=prod_unmet, web_search_requests=2,
        ),
        "moonshotai/kimi-k2.6": _grounded(
            provider="openrouter", model="moonshotai/kimi-k2.6",
            unmet_hit=False, web_search_requests=2,
        ),
        "xiaomi/mimo-v2.5-pro": _grounded(
            provider="openrouter", model="xiaomi/mimo-v2.5-pro",
            unmet_hit=False, web_search_requests=2,
        ),
    }


def test_run_bakeoff_reports_parity_for_every_candidate(bakeoff_module, monkeypatch):
    from morning_signal.config import load_config

    monkeypatch.setattr(bakeoff_module, "LLMClient", _dispatcher(_all_pass_plan()))

    config = load_config()
    record = bakeoff_module.run_bakeoff(config, "2026-07-06", "am")

    assert record["prod"]["unmet_topics"] == []
    assert set(record["candidates"]) == {"kimi-k2.6", "mimo-v2.5-pro"}
    for label in ("kimi-k2.6", "mimo-v2.5-pro"):
        candidate = record["candidates"][label]
        assert candidate["unmet_topics"] == []
        assert candidate["parity"]["unmet_topics_match"] is True
        assert candidate["parity"]["candidate_strictly_worse"] is False
        assert candidate["parity"]["both_met_min_web_searches"] is True


def test_run_bakeoff_flags_one_candidate_strictly_worse(bakeoff_module, monkeypatch):
    plan = _all_pass_plan()
    plan["moonshotai/kimi-k2.6"] = _grounded(
        provider="openrouter", model="moonshotai/kimi-k2.6",
        unmet_hit=True, web_search_requests=2,
    )
    monkeypatch.setattr(bakeoff_module, "LLMClient", _dispatcher(plan))

    from morning_signal.config import load_config
    config = load_config()
    record = bakeoff_module.run_bakeoff(config, "2026-07-06", "am")

    kimi = record["candidates"]["kimi-k2.6"]
    mimo = record["candidates"]["mimo-v2.5-pro"]
    assert kimi["unmet_topics"] == ["Political pulse"]
    assert kimi["parity"]["candidate_strictly_worse"] is True
    assert mimo["unmet_topics"] == []
    assert mimo["parity"]["candidate_strictly_worse"] is False


def test_candidate_specs_carry_reasoning_exclude(bakeoff_module, monkeypatch):
    """Both candidates must set reasoning={"exclude": True} — the fix for
    the empty-content bug found 2026-07-06 (krepis#16)."""
    seen_specs = []

    class _Client:
        def __init__(self, spec, **kw):
            seen_specs.append(spec)
            self.spec = spec

        def complete_grounded(self, **kw):
            plan = _all_pass_plan()
            key = self.spec.model if self.spec.provider == "openrouter" else self.spec.provider
            return plan[key]

    monkeypatch.setattr(bakeoff_module, "LLMClient", _Client)

    from morning_signal.config import load_config
    config = load_config()
    bakeoff_module.run_bakeoff(config, "2026-07-06", "am")

    candidate_specs = [s for s in seen_specs if s.provider == "openrouter"]
    assert len(candidate_specs) == 2
    for spec in candidate_specs:
        assert spec.reasoning == {"exclude": True}


def test_main_fails_without_openrouter_key(bakeoff_module, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["oss_bakeoff.py", "--date", "2026-07-06"])
    assert bakeoff_module.main() == 1


def test_main_writes_jsonl_on_success(bakeoff_module, monkeypatch, tmp_path):
    monkeypatch.setattr(bakeoff_module, "LLMClient", _dispatcher(_all_pass_plan()))
    log_dir = tmp_path / "bakeoff_out"
    monkeypatch.setenv(bakeoff_module.BAKEOFF_LOG_DIR_ENV, str(log_dir))
    monkeypatch.setattr(sys, "argv", ["oss_bakeoff.py", "--date", "2026-07-06", "--edition", "am"])

    assert bakeoff_module.main() == 0

    out_path = log_dir / "2026-07-06-am.bakeoff.jsonl"
    assert out_path.exists()
    record = json.loads(out_path.read_text().strip().splitlines()[0])
    assert record["date"] == "2026-07-06"
    assert record["edition"] == "am"
    assert record["candidates"]["kimi-k2.6"]["parity"]["unmet_topics_match"] is True
    assert record["candidates"]["mimo-v2.5-pro"]["parity"]["unmet_topics_match"] is True


def test_main_appends_on_repeated_runs(bakeoff_module, monkeypatch, tmp_path):
    monkeypatch.setattr(bakeoff_module, "LLMClient", _dispatcher(_all_pass_plan()))
    log_dir = tmp_path / "bakeoff_out"
    monkeypatch.setenv(bakeoff_module.BAKEOFF_LOG_DIR_ENV, str(log_dir))
    monkeypatch.setattr(sys, "argv", ["oss_bakeoff.py", "--date", "2026-07-06", "--edition", "am"])

    assert bakeoff_module.main() == 0
    assert bakeoff_module.main() == 0

    out_path = log_dir / "2026-07-06-am.bakeoff.jsonl"
    assert len(out_path.read_text().strip().splitlines()) == 2
