"""Tests for ``scripts/canary.py`` (ROADMAP L380 Phase A).

The script itself dispatches a live Anthropic ``messages.create()`` call
in production — these tests stub the network boundary and exercise the
exit-code matrix (no API key / SSM failure / config load failure /
payload-build failure / HTTP 400 / HTTP 5xx / OK).

Mirrors ``tests/live_api_smoke.py``'s philosophy: validate the
producer-side surface (config + prompt + payload-build chain) without
hitting the real API. Live API coverage is the CI smoke
(``.github/workflows/live-api-smoke.yml``) plus production runtime.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "src"))


@pytest.fixture
def canary_module(monkeypatch, tmp_path: Path):
    """Reload ``canary`` after wiring config + prompt + env to tmp paths.

    The fixture writes a minimal ``config.yaml`` + ``prompt.md`` /
    ``prompt_weekend.md`` / ``prompt_public.md`` triple, points
    ``morning_signal.config`` module-level paths at them, sets
    ``ANTHROPIC_API_KEY``, and unsets ``MORNING_SIGNAL_USE_SSM`` so the
    SSM bootstrap is a no-op (the live-SSM path is exercised in
    ``tests/test_aws_paths.py``).
    """
    from morning_signal import config as _config_mod

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "s3_bucket: test-bucket\n"
        "claude_model: claude-sonnet-4-6\n"
        "max_tokens: 4096\n"
        "web_search_max_uses: 20\n"
        "public_topics_mode: false\n"
    )
    (tmp_path / "prompt.md").write_text("Weekday system prompt.\n")
    (tmp_path / "prompt_weekend.md").write_text("Weekend system prompt.\n")
    (tmp_path / "prompt_public.md").write_text("Public-topics system prompt.\n")

    monkeypatch.setattr(_config_mod, "CONFIG_FILE", cfg_path)
    monkeypatch.setattr(_config_mod, "PROMPT_FILE", tmp_path / "prompt.md")
    monkeypatch.setattr(
        _config_mod, "PROMPT_WEEKEND_FILE", tmp_path / "prompt_weekend.md"
    )
    monkeypatch.setattr(
        _config_mod, "PROMPT_PUBLIC_FILE", tmp_path / "prompt_public.md"
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-abc")
    monkeypatch.delenv("MORNING_SIGNAL_USE_SSM", raising=False)

    if "canary" in sys.modules:
        del sys.modules["canary"]
    return importlib.import_module("canary")


def test_canary_returns_1_when_api_key_missing(canary_module, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert canary_module.main() == 1


def test_canary_returns_1_on_invalid_edition(canary_module, monkeypatch):
    monkeypatch.setenv("MORNING_SIGNAL_CANARY_EDITION", "midnight")
    assert canary_module.main() == 1


def test_canary_builds_production_shape_payload(canary_module):
    """The canary's payload MUST mirror ``generate_script``'s shape.

    Pins the load-bearing invariants: server-tool present, cache_control
    on the system block, max_tokens=1, no assistant prefill in messages.
    """
    cfg = {
        "claude_model": "claude-sonnet-4-6",
        "max_tokens": 4096,
        "web_search_max_uses": 20,
        "public_topics_mode": False,
    }
    payload = canary_module._build_canary_payload(cfg, "2026-05-28", "am")

    assert payload["max_tokens"] == 1
    assert payload["model"] == "claude-sonnet-4-6"
    assert any(
        t.get("type", "").startswith("web_search") for t in payload["tools"]
    )
    sys_block = payload["system"]
    assert isinstance(sys_block, list) and sys_block
    assert sys_block[0].get("cache_control") == {"type": "ephemeral"}
    for msg in payload["messages"]:
        assert msg["role"] != "assistant"


def test_canary_returns_0_on_successful_dispatch(canary_module):
    fake_resp = MagicMock()
    fake_resp.stop_reason = "max_tokens"
    fake_resp.usage.input_tokens = 1234
    fake_resp.usage.output_tokens = 1

    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_resp

    fake_anthropic = MagicMock()
    fake_anthropic.Anthropic.return_value = fake_client
    fake_anthropic.BadRequestError = type("BadRequestError", (Exception,), {})
    fake_anthropic.APIStatusError = type("APIStatusError", (Exception,), {})

    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        assert canary_module.main() == 0
    fake_client.messages.create.assert_called_once()


def test_canary_returns_1_on_anthropic_400(canary_module):
    class _BadRequest(Exception):
        pass

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = _BadRequest(
        "This model does not support assistant message prefill."
    )

    fake_anthropic = MagicMock()
    fake_anthropic.Anthropic.return_value = fake_client
    fake_anthropic.BadRequestError = _BadRequest
    fake_anthropic.APIStatusError = type("APIStatusError", (Exception,), {})

    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        assert canary_module.main() == 1


def test_canary_returns_1_on_anthropic_5xx(canary_module):
    class _APIStatusError(Exception):
        def __init__(self, status_code: int, message: str) -> None:
            super().__init__(message)
            self.status_code = status_code

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = _APIStatusError(
        503, "service unavailable"
    )

    fake_anthropic = MagicMock()
    fake_anthropic.Anthropic.return_value = fake_client
    fake_anthropic.BadRequestError = type("BadRequestError", (Exception,), {})
    fake_anthropic.APIStatusError = _APIStatusError

    with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
        assert canary_module.main() == 1
