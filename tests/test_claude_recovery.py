"""Tests for the self-healing coverage-recovery pass in ``generate_script``.

The recurring failure (4× by 2026-06-29) is that the model silently drops a
no-digest political segment despite available search budget. The recovery pass
fires ONE bounded regeneration that forces the dropped segment(s), adopting the
recovered draft only if it covers strictly more — then falls back to the
publish+alert (or hard-abort) policy if recovery can't fix it.

These tests drive ``generate_script`` with duck-typed fake Anthropic responses
so no real ``anthropic`` call (or network) happens.
"""

from __future__ import annotations

import pytest

from morning_signal import claude
from morning_signal.claude import _coverage_recovery_directive


# ── duck-typed Anthropic response fakes ──────────────────────────────────────


class _Blk:
    def __init__(self, **kw):
        self.type = kw.get("type")
        for k, v in kw.items():
            setattr(self, k, v)


def _text(t: str) -> _Blk:
    return _Blk(type="text", text=t)


def _search(block_id: str, query: str) -> list[_Blk]:
    """A server_tool_use + its paired result block (one URL)."""
    return [
        _Blk(type="server_tool_use", name="web_search", id=block_id,
             input={"query": query}),
        _Blk(type="web_search_tool_result", tool_use_id=block_id,
             content=[_Blk(url="https://example.com/x")]),
    ]


class _Resp:
    def __init__(self, content):
        self.content = content


class _FakeMessages:
    def __init__(self, responses):
        self._responses = list(responses)
        self.payloads = []

    def create(self, **payload):
        self.payloads.append(payload)
        if not self._responses:
            raise AssertionError("messages.create called more times than expected")
        return self._responses.pop(0)


class _FakeClient:
    def __init__(self, responses):
        self.messages = _FakeMessages(responses)


# Two political topics: Trump (covered on pass 1) + MAGA (dropped on pass 1).
TOPICS = [
    {"name": "Trump", "keywords": ["trump"], "editions": ["am", "pm"]},
    {"name": "MAGA Pulse", "keywords": ["shapiro", "bannon"], "editions": ["am", "pm"]},
]

# Pass-1 draft: searched + wrote Trump, never searched/wrote MAGA → MAGA unmet.
DEGRADED = _Resp(
    _search("s1", "trump truth social posts today")
    + [_text("Welcome to Morning Signal. On Trump, he posted on Truth Social. "
             "Seattle weather: cloudy.")]
)
# Recovered draft: both segments searched + written → fully covered.
RECOVERED = _Resp(
    _search("s1", "trump truth social posts")
    + _search("s2", "ben shapiro steve bannon maga reaction")
    + [_text("Welcome to Morning Signal. On Trump, he posted on Truth Social. "
             "On the MAGA pulse, Shapiro and Bannon weighed in. Seattle: sunny.")]
)


@pytest.fixture
def patched(monkeypatch):
    """Stub out prompt loading, news context, telemetry IO, the trading-day
    check, and the alert sink. Returns a dict the test can inspect."""
    state = {"alerts": []}
    monkeypatch.setattr(claude, "load_prompt", lambda weekend=False: "SYSTEM PROMPT")
    monkeypatch.setattr(claude, "load_news_context", lambda config, run_date=None: "")
    monkeypatch.setattr(claude, "is_non_trading_day", lambda date_str: False)
    monkeypatch.setattr(claude, "record_call_cost", lambda **kw: None)
    monkeypatch.setattr(claude, "record_searches",
                        lambda **kw: len(claude.extract_searches(kw["msg"])))

    def _fake_alert(config, edition, edition_label, date_str, unmet, n, budget):
        state["alerts"].append(list(unmet))

    monkeypatch.setattr(claude, "_alert_degraded_coverage", _fake_alert)
    return state


def _run(monkeypatch, responses, *, config):
    client = _FakeClient(responses)
    monkeypatch.setattr("anthropic.Anthropic", lambda **kw: client)
    script = claude.generate_script(config, "2026-06-29", "am")
    return script, client


def _base_config(**overrides):
    cfg = {
        "claude_model": "claude-haiku-4-5",
        "max_tokens": 256,
        "web_search_max_uses": 20,
        "min_web_searches": 1,
        "required_search_topics": TOPICS,
    }
    cfg.update(overrides)
    return cfg


def test_recovery_fixes_dropped_segment_and_suppresses_alert(monkeypatch, patched):
    script, client = _run(monkeypatch, [DEGRADED, RECOVERED], config=_base_config())
    # Recovered draft adopted (contains the MAGA segment).
    assert "Shapiro" in script and "Bannon" in script
    # Two generations: original + one recovery.
    assert len(client.messages.payloads) == 2
    # Full coverage after recovery → no degraded-coverage alert.
    assert patched["alerts"] == []


def test_recovery_user_message_names_the_dropped_segment(monkeypatch, patched):
    _, client = _run(monkeypatch, [DEGRADED, RECOVERED], config=_base_config())
    # The 2nd (recovery) payload's user message must carry the forcing
    # directive naming the dropped segment + its figures.
    recovery_payload = repr(client.messages.payloads[1])
    assert "CRITICAL COVERAGE CORRECTION" in recovery_payload
    assert "MAGA Pulse" in recovery_payload
    assert "shapiro" in recovery_payload
    # The first (original) payload must NOT carry it.
    assert "CRITICAL COVERAGE CORRECTION" not in repr(client.messages.payloads[0])


def test_recovery_forces_websearch_via_tool_choice(monkeypatch, patched):
    _, client = _run(monkeypatch, [DEGRADED, RECOVERED], config=_base_config())
    # First (natural) pass must NOT force a tool; recovery pass MUST force
    # web_search via tool_choice so coverage is deterministic, not asked-for.
    assert "tool_choice" not in client.messages.payloads[0]
    assert client.messages.payloads[1]["tool_choice"] == {
        "type": "tool", "name": "web_search",
    }


def test_recovery_failure_keeps_original_and_alerts(monkeypatch, patched):
    # Recovery pass ALSO drops MAGA → keep original draft, publish + alert.
    script, client = _run(monkeypatch, [DEGRADED, DEGRADED], config=_base_config())
    assert "Shapiro" not in script  # original (degraded) draft kept
    assert len(client.messages.payloads) == 2  # original + one recovery attempt
    assert patched["alerts"] == [["MAGA Pulse"]]


def test_recovery_disabled_skips_retry_and_alerts(monkeypatch, patched):
    script, client = _run(
        monkeypatch, [DEGRADED],
        config=_base_config(required_search_topics_recover=False),
    )
    assert len(client.messages.payloads) == 1  # no recovery pass
    assert patched["alerts"] == [["MAGA Pulse"]]


def test_full_coverage_first_pass_needs_no_recovery(monkeypatch, patched):
    script, client = _run(monkeypatch, [RECOVERED], config=_base_config())
    assert len(client.messages.payloads) == 1
    assert patched["alerts"] == []


def test_fatal_aborts_only_after_failed_recovery(monkeypatch, patched):
    # fatal=True still runs recovery first; aborts only if it can't cover.
    with pytest.raises(RuntimeError, match="MAGA Pulse"):
        _run(monkeypatch, [DEGRADED, DEGRADED],
             config=_base_config(required_search_topics_fatal=True))


def test_fatal_recovery_succeeds_publishes(monkeypatch, patched):
    # fatal=True but recovery covers everything → publish, no abort.
    script, client = _run(
        monkeypatch, [DEGRADED, RECOVERED],
        config=_base_config(required_search_topics_fatal=True),
    )
    assert "Shapiro" in script
    assert patched["alerts"] == []


# ── _coverage_recovery_directive ─────────────────────────────────────────────


def test_directive_names_segments_and_figures():
    directive = _coverage_recovery_directive(["MAGA Pulse"], TOPICS, "am")
    assert "MAGA Pulse" in directive
    assert "shapiro" in directive and "bannon" in directive
    assert "dedicated web search" in directive


def test_directive_handles_unknown_topic_name_gracefully():
    directive = _coverage_recovery_directive(["Mystery"], TOPICS, "am")
    assert "Mystery" in directive  # still instructs a dedicated search
