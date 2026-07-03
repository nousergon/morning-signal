"""Tests for the optional per-date scheduled content overrides.

Covers ``schedule_override.load_schedule_override`` (disabled -> None with
zero S3 calls, fail-soft + alert posture, entry/edition matching,
normalization), the directive/guard builders (incl. the no-``editions``-key
invariant on the dynamic guard), the applied marker, the
``claude.generate_script`` wiring (override suppresses config topics,
extend composes, weekend guard enforcement, zero-behavior-change when
absent), the ``episode.main`` scheduled-skip guard, and the watchdog's
scheduled-skip suppression.
"""

from __future__ import annotations

import json
import sys

import boto3
import pytest
from moto import mock_aws

from morning_signal import aws as _aws
from morning_signal import claude as _claude
from morning_signal import config as _config
from morning_signal import schedule_override as so

REGION = "us-west-2"
BUCKET = "sched-bucket"


@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)


def _manifest(entries: dict) -> dict:
    return {"schema_version": 1, "entries": entries}


def _seed(manifest: dict | str, bucket: str = BUCKET, key: str | None = None) -> None:
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(
        Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": REGION}
    )
    if manifest is not None:
        body = manifest if isinstance(manifest, str) else json.dumps(manifest)
        s3.put_object(Bucket=bucket, Key=key or so.DEFAULT_S3_KEY, Body=body.encode())


def _cfg(**overrides) -> dict:
    cfg = {
        "s3_region": REGION,
        "s3_bucket": BUCKET,
        "schedule": {"enabled": True},
    }
    cfg.update(overrides)
    return cfg


OVERRIDE_ENTRY = {
    "mode": "override",
    "topic": "Quantum computing breakthroughs",
    "guidance": "Focus on error correction.",
    "keywords": ["quantum"],
    "min_searches": 1,
}


# ── disabled / default-OFF (the OSS-safe guarantee) ──────────────────────────


def test_disabled_returns_none_no_s3_call(monkeypatch):
    """Absent/disabled schedule block = None with ZERO network activity —
    the basic-form zero-behavior-change guarantee."""

    def _boom(*a, **kw):
        raise AssertionError("S3 client must not be constructed when disabled")

    monkeypatch.setattr(_aws, "_aws_client", _boom)
    assert so.load_schedule_override({}, "2026-07-04", "am") is None
    assert (
        so.load_schedule_override(
            {"schedule": {"enabled": False}}, "2026-07-04", "am"
        )
        is None
    )


# ── missing manifest vs read failures ────────────────────────────────────────


@mock_aws
def test_missing_manifest_returns_none_without_alert(aws_env, monkeypatch):
    """An unseeded schedule is normal — INFO, no alert."""
    _seed(None)  # bucket exists, no manifest object
    alerts: list[str] = []
    monkeypatch.setattr(so, "_alert_schedule_failure", lambda c, e, m: alerts.append(m))
    assert so.load_schedule_override(_cfg(), "2026-07-04", "am") is None
    assert alerts == []


@mock_aws
def test_read_error_fails_soft_and_alerts(aws_env, monkeypatch, caplog):
    """Bucket doesn't exist → fail-soft (None) + WARN + alert."""
    alerts: list[str] = []
    monkeypatch.setattr(so, "_alert_schedule_failure", lambda c, e, m: alerts.append(m))
    with caplog.at_level("WARNING"):
        assert so.load_schedule_override(_cfg(), "2026-07-04", "am") is None
    assert any("could not load manifest" in r.message for r in caplog.records)
    assert len(alerts) == 1


@mock_aws
def test_alert_on_failure_false_suppresses_alert_keeps_warn(
    aws_env, monkeypatch, caplog
):
    alerts: list[str] = []
    monkeypatch.setattr(so, "_alert_schedule_failure", lambda c, e, m: alerts.append(m))
    with caplog.at_level("WARNING"):
        assert (
            so.load_schedule_override(
                _cfg(), "2026-07-04", "am", alert_on_failure=False
            )
            is None
        )
    assert any("could not load manifest" in r.message for r in caplog.records)
    assert alerts == []


@mock_aws
def test_malformed_json_fails_soft_and_alerts(aws_env, monkeypatch):
    _seed("{not json")
    alerts: list[str] = []
    monkeypatch.setattr(so, "_alert_schedule_failure", lambda c, e, m: alerts.append(m))
    assert so.load_schedule_override(_cfg(), "2026-07-04", "am") is None
    assert len(alerts) == 1


@mock_aws
def test_wrong_schema_version_fails_soft_and_alerts(aws_env, monkeypatch):
    _seed({"schema_version": 2, "entries": {}})
    alerts: list[str] = []
    monkeypatch.setattr(so, "_alert_schedule_failure", lambda c, e, m: alerts.append(m))
    assert so.load_schedule_override(_cfg(), "2026-07-04", "am") is None
    assert len(alerts) == 1
    assert "schema_version" in alerts[0]


def test_enabled_without_bucket_fails_soft_and_alerts(monkeypatch):
    alerts: list[str] = []
    monkeypatch.setattr(so, "_alert_schedule_failure", lambda c, e, m: alerts.append(m))
    cfg = {"schedule": {"enabled": True}}  # no s3_bucket anywhere
    assert so.load_schedule_override(cfg, "2026-07-04", "am") is None
    assert len(alerts) == 1


# ── entry matching + normalization ───────────────────────────────────────────


@mock_aws
def test_entry_matched_for_date_and_default_am_edition(aws_env):
    _seed(_manifest({"2026-07-04": dict(OVERRIDE_ENTRY)}))
    entry = so.load_schedule_override(_cfg(), "2026-07-04", "am")
    assert entry is not None
    assert entry["mode"] == "override"
    assert entry["topic"] == "Quantum computing breakthroughs"
    assert entry["keywords"] == ["quantum"]
    assert entry["min_searches"] == 1


@mock_aws
def test_entry_default_editions_exclude_pm_for_override(aws_env):
    _seed(_manifest({"2026-07-04": dict(OVERRIDE_ENTRY)}))
    assert so.load_schedule_override(_cfg(), "2026-07-04", "pm") is None


@mock_aws
def test_skip_entry_default_editions_match_both(aws_env):
    _seed(_manifest({"2026-07-09": {"mode": "skip", "guidance": "travel"}}))
    for edition in ("am", "pm"):
        entry = so.load_schedule_override(_cfg(), "2026-07-09", edition)
        assert entry is not None and entry["mode"] == "skip"


@mock_aws
def test_no_entry_for_other_dates(aws_env):
    _seed(_manifest({"2026-07-04": dict(OVERRIDE_ENTRY)}))
    assert so.load_schedule_override(_cfg(), "2026-07-05", "am") is None


@mock_aws
def test_custom_bucket_and_key(aws_env):
    _seed(
        _manifest({"2026-07-04": dict(OVERRIDE_ENTRY)}),
        bucket="other-bucket",
        key="custom/sched.json",
    )
    cfg = _cfg(
        schedule={
            "enabled": True,
            "s3_bucket": "other-bucket",
            "s3_key": "custom/sched.json",
        }
    )
    assert so.load_schedule_override(cfg, "2026-07-04", "am") is not None


@mock_aws
def test_derived_keywords_and_default_min_searches_when_absent(aws_env):
    _seed(
        _manifest(
            {
                "2026-07-04": {
                    "mode": "override",
                    "topic": "State of the art in financial machine-learning research",
                }
            }
        )
    )
    entry = so.load_schedule_override(_cfg(), "2026-07-04", "am")
    # Filler ("state", "the", short tokens) dropped; substance kept.
    assert entry["keywords"] == ["financial", "machine", "learning", "research"]
    assert entry["min_searches"] == 3  # override default


# ── directive + guard builders ───────────────────────────────────────────────


def _norm(**overrides) -> dict:
    entry = dict(OVERRIDE_ENTRY)
    entry.update(overrides)
    return so._normalize_entry(entry)


def test_override_directive_replaces_programming_wording():
    text = so.format_schedule_directive(_norm(), "WEEKEND")
    assert "REPLACES REGULAR PROGRAMMING" in text
    assert "Quantum computing breakthroughs" in text
    assert "Do NOT produce the regular segment lineup" in text
    assert "Focus on error correction." in text
    # Generic supersession — must not name weekday-only segments (the
    # directive lands on the WEEKEND prompt for weekend deep dives).
    assert "web search" in text.lower()


def test_extend_directive_wording():
    text = so.format_schedule_directive(_norm(mode="extend", guidance=""), "MORNING")
    assert "SCHEDULED EXTRA SEGMENT" in text
    assert "In ADDITION to the full regular segment lineup" in text
    assert "(none)" in text  # empty guidance rendered explicitly


def test_skip_directive_is_empty():
    assert so.format_schedule_directive(_norm(mode="skip", topic=""), "MORNING") == ""


def test_derived_topic_guard_has_no_editions_key():
    """THE load-bearing invariant: coverage filters by effective_edition
    ("weekend" on non-trading days) — an editions-scoped guard would be
    silently skipped on every weekend deep dive."""
    guard = so.derived_topic_guard(_norm())
    assert guard is not None
    assert "editions" not in guard
    assert guard["keywords"] == ["quantum"]
    assert guard["min_matches"] == 1


def test_derived_topic_guard_none_for_skip_and_keywordless():
    assert so.derived_topic_guard(_norm(mode="skip", topic="")) is None
    entry = _norm()
    entry["keywords"] = []
    assert so.derived_topic_guard(entry) is None


# ── applied marker ───────────────────────────────────────────────────────────


@mock_aws
def test_record_applied_writes_marker(aws_env):
    _seed(None)
    so.record_applied(_cfg(), "2026-07-04", "am", _norm())
    s3 = boto3.client("s3", region_name=REGION)
    body = s3.get_object(
        Bucket=BUCKET, Key=f"{so.APPLIED_PREFIX}2026-07-04-am.json"
    )["Body"].read()
    marker = json.loads(body)
    assert marker["mode"] == "override"
    assert marker["topic"] == "Quantum computing breakthroughs"
    assert marker["date"] == "2026-07-04"
    assert marker["edition"] == "am"
    assert marker["schema_version"] == so.SCHEMA_VERSION
    assert marker["generator_version"]
    assert marker["applied_at_utc"]


def test_record_applied_failure_warns_not_raises(caplog):
    """No AWS backing at all — the marker write must degrade to a WARN,
    never block TTS/publish."""
    with caplog.at_level("WARNING"):
        so.record_applied(_cfg(), "2026-07-04", "am", _norm())
    assert any("applied marker" in r.message for r in caplog.records)


# ── generate_script wiring (duck-typed Anthropic fakes) ──────────────────────


class _Blk:
    def __init__(self, **kw):
        self.type = kw.get("type")
        for k, v in kw.items():
            setattr(self, k, v)


def _text(t: str) -> _Blk:
    return _Blk(type="text", text=t)


def _search(block_id: str, query: str) -> list[_Blk]:
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


CONFIG_TOPICS = [
    {"name": "Political pulse", "keywords": ["truth social"], "editions": ["am"]},
]


@pytest.fixture
def gs_patched(monkeypatch):
    """Stub prompt/news/telemetry/trading-day/alert/marker surfaces around
    generate_script. Returns inspectable state."""
    state = {"alerts": [], "applied": [], "weekend": False}
    monkeypatch.setattr(_claude, "load_prompt", lambda weekend=False: "SYSTEM PROMPT")
    monkeypatch.setattr(_claude, "load_news_context", lambda config, run_date=None: "")
    monkeypatch.setattr(
        _claude, "is_non_trading_day", lambda date_str: state["weekend"]
    )
    monkeypatch.setattr(_claude, "record_call_cost", lambda **kw: None)
    monkeypatch.setattr(
        _claude, "record_searches",
        lambda **kw: len(_claude.extract_searches(kw["msg"])),
    )
    monkeypatch.setattr(
        _claude, "_alert_degraded_coverage",
        lambda config, edition, edition_label, date_str, unmet, n, budget:
        state["alerts"].append(list(unmet)),
    )
    monkeypatch.setattr(
        _claude, "record_applied",
        lambda config, date_str, edition, entry: state["applied"].append(
            (date_str, edition, entry["mode"])
        ),
    )
    return state


def _gs_config(**overrides) -> dict:
    cfg = {
        "claude_model": "claude-haiku-4-5",
        "max_tokens": 256,
        "web_search_max_uses": 20,
        "min_web_searches": 1,
        "required_search_topics": [dict(t) for t in CONFIG_TOPICS],
    }
    cfg.update(overrides)
    return cfg


def _run_gs(monkeypatch, responses, *, config, **kwargs):
    client = _FakeClient(responses)
    monkeypatch.setattr("anthropic.Anthropic", lambda **kw: client)
    script = _claude.generate_script(config, "2026-07-04", "am", **kwargs)
    return script, client


QUANTUM_OK = _Resp(
    _search("s1", "quantum computing error correction news")
    + [_text("Welcome to Morning Signal. Today: quantum computing, deeply.")]
)


def test_generate_script_injects_override_and_suppresses_config_topics(
    monkeypatch, gs_patched
):
    """Override mode: directive lands in the user message, the generate-
    instruction switches to the deep-dive variant, and the config-declared
    topics are suppressed (one generation pass, no recovery, no degraded-
    coverage alert despite 'truth social' never being searched)."""
    entry = so._normalize_entry(dict(OVERRIDE_ENTRY))
    script, client = _run_gs(
        monkeypatch, [QUANTUM_OK], config=_gs_config(), schedule_entry=entry
    )
    assert len(client.messages.payloads) == 1
    user_content = client.messages.payloads[0]["messages"][0]["content"]
    assert "SCHEDULED DEEP-DIVE OVERRIDE" in user_content
    assert "special deep-dive episode" in user_content
    assert "News Window" not in user_content
    # Opener instruction still LAST in the user message.
    assert user_content.rstrip().endswith("Welcome to Morning Signal.")
    assert gs_patched["alerts"] == []
    assert gs_patched["applied"] == [("2026-07-04", "am", "override")]
    assert "quantum" in script.lower()


def test_generate_script_extend_keeps_config_topics_and_adds_guard(
    monkeypatch, gs_patched
):
    """Extend mode: config topics still enforced ALONGSIDE the dynamic
    guard — a response covering both passes in one shot."""
    entry = so._normalize_entry(dict(OVERRIDE_ENTRY, mode="extend"))
    both_covered = _Resp(
        _search("s1", "trump truth social posts today")
        + _search("s2", "quantum computing news")
        + [_text("Welcome to Morning Signal. Truth Social pulse. Then quantum extras.")]
    )
    script, client = _run_gs(
        monkeypatch, [both_covered], config=_gs_config(), schedule_entry=entry
    )
    assert len(client.messages.payloads) == 1
    user_content = client.messages.payloads[0]["messages"][0]["content"]
    assert "SCHEDULED EXTRA SEGMENT" in user_content
    assert "News Window" in user_content  # regular generate-instruction kept
    assert gs_patched["alerts"] == []
    assert gs_patched["applied"] == [("2026-07-04", "am", "extend")]


def test_generate_script_weekend_override_guard_enforced(monkeypatch, gs_patched):
    """On a weekend (effective_edition='weekend'), the dynamic guard still
    fires: a draft that never searched the deep-dive topic triggers the
    forced-web_search recovery pass, whose covering draft is adopted."""
    gs_patched["weekend"] = True
    entry = so._normalize_entry(dict(OVERRIDE_ENTRY))
    uncovered = _Resp(
        _search("s1", "unrelated markets news")
        + [_text("Welcome to Morning Signal, weekend edition. Markets rambling.")]
    )
    recovered = _Resp(
        _search("s1", "quantum computing error correction breakthroughs")
        + [_text("Welcome to Morning Signal, weekend edition. Quantum, deeply.")]
    )
    script, client = _run_gs(
        monkeypatch, [uncovered, recovered], config=_gs_config(), schedule_entry=entry
    )
    assert len(client.messages.payloads) == 2  # original + recovery
    assert client.messages.payloads[1]["tool_choice"] == {
        "type": "tool", "name": "web_search",
    }
    assert "quantum" in script.lower()
    assert gs_patched["alerts"] == []
    assert gs_patched["applied"] == [("2026-07-04", "am", "override")]


def test_generate_script_no_schedule_zero_behavior_change(monkeypatch, gs_patched):
    """schedule_entry=None (or a self-load returning None): no directive,
    the original generate-instruction, no applied marker."""
    covered = _Resp(
        _search("s1", "trump truth social posts today")
        + [_text("Welcome to Morning Signal. Truth Social pulse covered.")]
    )
    monkeypatch.setattr(_claude, "load_schedule_override", lambda c, d, e: None)
    script, client = _run_gs(monkeypatch, [covered], config=_gs_config())
    user_content = client.messages.payloads[0]["messages"][0]["content"]
    assert "SCHEDULED" not in user_content
    assert "News Window" in user_content
    assert gs_patched["applied"] == []


def test_generate_script_sentinel_self_loads(monkeypatch, gs_patched):
    """Direct callers (no schedule_entry arg) self-load from config/S3."""
    entry = so._normalize_entry(dict(OVERRIDE_ENTRY))
    monkeypatch.setattr(
        _claude, "load_schedule_override", lambda c, d, e: dict(entry)
    )
    script, client = _run_gs(monkeypatch, [QUANTUM_OK], config=_gs_config())
    user_content = client.messages.payloads[0]["messages"][0]["content"]
    assert "SCHEDULED DEEP-DIVE OVERRIDE" in user_content


def test_generate_script_self_loaded_skip_degrades_to_regular(
    monkeypatch, gs_patched
):
    """A skip entry reaching generate_script directly (the orchestrator owns
    the skip decision) generates regular programming, no marker."""
    skip_entry = so._normalize_entry({"mode": "skip"})
    monkeypatch.setattr(
        _claude, "load_schedule_override", lambda c, d, e: dict(skip_entry)
    )
    covered = _Resp(
        _search("s1", "trump truth social posts today")
        + [_text("Welcome to Morning Signal. Regular programming.")]
    )
    script, client = _run_gs(monkeypatch, [covered], config=_gs_config())
    user_content = client.messages.payloads[0]["messages"][0]["content"]
    assert "SCHEDULED" not in user_content
    assert gs_patched["applied"] == []


# ── episode.main scheduled-skip guard ────────────────────────────────────────


@pytest.fixture
def _episode_env(monkeypatch, tmp_path):
    """Neutralize bootstrap + point dirs at tmp (mirrors test_skip_dates)."""
    from morning_signal import episode as _episode

    config = {"s3_bucket": BUCKET, "s3_region": REGION, "schedule": {"enabled": True}}
    monkeypatch.setattr(_aws, "_load_runner_session", lambda: None)
    monkeypatch.setattr(_aws, "_maybe_load_from_ssm", lambda: None)
    monkeypatch.setattr(_config, "load_config", lambda: config)
    monkeypatch.setattr(_config, "EPISODES_DIR", tmp_path / "episodes")
    monkeypatch.setattr(_config, "SCRIPTS_DIR", tmp_path / "scripts")

    state = {"generate_calls": [], "applied": []}

    def _capture_generate(config, date_str, edition, schedule_entry=None):
        state["generate_calls"].append(schedule_entry)
        raise AssertionError("stop after generate_script capture")

    monkeypatch.setattr(_episode, "generate_script", _capture_generate)
    monkeypatch.setattr(
        _episode, "record_applied",
        lambda config, d, e, entry: state["applied"].append((d, e, entry["mode"])),
    )
    return _episode, state


def test_episode_main_scheduled_skip_no_generation(_episode_env, monkeypatch):
    episode, state = _episode_env
    skip_entry = so._normalize_entry({"mode": "skip", "guidance": "travel"})
    monkeypatch.setattr(
        episode, "load_schedule_override",
        lambda config, d, e, alert_on_failure=True: dict(skip_entry),
    )
    monkeypatch.setattr(
        sys, "argv",
        ["generate_episode.py", "--date", "2026-07-09", "--edition", "am"],
    )
    episode.main()  # clean return: no generation, marker recorded
    assert state["generate_calls"] == []
    assert state["applied"] == [("2026-07-09", "am", "skip")]


def test_episode_main_force_overrides_scheduled_skip(_episode_env, monkeypatch):
    episode, state = _episode_env
    skip_entry = so._normalize_entry({"mode": "skip"})
    monkeypatch.setattr(
        episode, "load_schedule_override",
        lambda config, d, e, alert_on_failure=True: dict(skip_entry),
    )
    monkeypatch.setattr(
        sys, "argv",
        ["generate_episode.py", "--date", "2026-07-09", "--edition", "am", "--force"],
    )
    with pytest.raises(AssertionError, match="stop after generate_script"):
        episode.main()
    # Forced past a skip = regular programming: entry explicitly None.
    assert state["generate_calls"] == [None]
    assert state["applied"] == []


def test_episode_main_passes_override_entry_to_generate(_episode_env, monkeypatch):
    """One manifest read per run: episode.main's loaded entry rides into
    generate_script instead of being re-fetched."""
    episode, state = _episode_env
    entry = so._normalize_entry(dict(OVERRIDE_ENTRY))
    monkeypatch.setattr(
        episode, "load_schedule_override",
        lambda config, d, e, alert_on_failure=True: dict(entry),
    )
    monkeypatch.setattr(
        sys, "argv",
        ["generate_episode.py", "--date", "2026-07-04", "--edition", "am"],
    )
    with pytest.raises(AssertionError, match="stop after generate_script"):
        episode.main()
    assert len(state["generate_calls"]) == 1
    assert state["generate_calls"][0]["mode"] == "override"


# ── watchdog scheduled-skip suppression ──────────────────────────────────────


@pytest.fixture
def _watchdog_env(monkeypatch):
    from morning_signal import episode as _episode

    config = {
        "s3_bucket": "test-podcast-bucket",
        "s3_region": REGION,
        "s3_prefix": "",
        "schedule": {"enabled": True},
    }
    monkeypatch.setattr(_aws, "_load_runner_session", lambda: None)
    monkeypatch.setattr(_aws, "_maybe_load_from_ssm", lambda: None)
    monkeypatch.setattr(_config, "load_config", lambda: config)
    monkeypatch.setattr(_episode, "_default_date", lambda: "2026-07-09")
    monkeypatch.setattr(_episode, "_default_edition", lambda: "am")


def test_watchdog_exits_zero_on_scheduled_skip(_watchdog_env, monkeypatch):
    """A manifest skip = expected absence — exit 0 before any freshness
    check (no moto bucket exists: reaching S3 would fail)."""
    from typer.testing import CliRunner

    from morning_signal.cli import app

    skip_entry = so._normalize_entry({"mode": "skip"})
    monkeypatch.setattr(
        so, "load_schedule_override",
        lambda config, d, e, alert_on_failure=True: dict(skip_entry),
    )
    result = CliRunner().invoke(app, ["watchdog"])
    assert result.exit_code == 0, result.output


def test_watchdog_still_checks_when_manifest_unreadable(_watchdog_env, monkeypatch):
    """Fail-OPEN: an unreadable manifest (loader returns None) must fall
    through to the freshness check — never silently suppress the page."""
    from typer.testing import CliRunner

    from morning_signal.cli import app

    monkeypatch.setattr(
        so, "load_schedule_override",
        lambda config, d, e, alert_on_failure=True: None,
    )
    # No AWS backing → the freshness check itself errors → exit != 0.
    result = CliRunner().invoke(app, ["watchdog"])
    assert result.exit_code != 0


def test_watchdog_override_entry_does_not_suppress(_watchdog_env, monkeypatch):
    """Only mode=skip suppresses — an override day still expects an episode."""
    from typer.testing import CliRunner

    from morning_signal.cli import app

    entry = so._normalize_entry(dict(OVERRIDE_ENTRY))
    monkeypatch.setattr(
        so, "load_schedule_override",
        lambda config, d, e, alert_on_failure=True: dict(entry),
    )
    result = CliRunner().invoke(app, ["watchdog"])
    assert result.exit_code != 0
