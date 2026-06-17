"""Tests for the optional pre-fetched news-context provider.

Covers ``news_context.load_news_context`` (disabled -> "", happy-path
section formatting, fail-soft on S3 / JSON errors) and the
``claude.generate_script`` injection wiring (block lands in the user
message between the edition sentence and the generate-instruction).
"""

from __future__ import annotations

import json
import sys
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from morning_signal import claude as _claude
from morning_signal import config as _config
from morning_signal import news_context as nc

REGION = "us-west-2"


@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)


_SAMPLE_DIGEST = {
    "schema_version": 1,
    "date": "2026-06-15",
    "generated_at": "2026-06-15T06:00:00+00:00",
    "sections": {
        "portfolio": [
            {
                "ticker": "AAPL",
                "title": "Apple unveils new chip",
                "source": "Reuters",
                "published": "2026-06-14",
                "excerpt": "Faster, cooler.",
                "sentiment": -0.1,
                "url": "https://example.com/aapl",
            }
        ],
        "macro": [
            {
                "title": "CPI comes in soft",
                "source": "BLS",
                "published": "2026-06-13",
                "excerpt": "Inflation eases.",
                "url": "https://example.com/cpi",
            }
        ],
        "tech": [
            {
                "title": "New model released",
                "source": "TechCrunch",
                "excerpt": "Big leap.",
            }
        ],
    },
}


def _seed_digest(bucket: str, key: str, digest: dict | str) -> None:
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(
        Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": REGION}
    )
    body = digest if isinstance(digest, str) else json.dumps(digest)
    s3.put_object(Bucket=bucket, Key=key, Body=body.encode())


# ── disabled / default-OFF ───────────────────────────────────────────────────


def test_disabled_returns_empty_when_block_absent():
    assert nc.load_news_context({}) == ""


def test_disabled_returns_empty_when_enabled_false():
    cfg = {"news_context": {"enabled": False, "s3_bucket": "b"}}
    assert nc.load_news_context(cfg) == ""


def test_enabled_without_bucket_fails_soft(caplog):
    cfg = {"news_context": {"enabled": True, "required": False}}
    with caplog.at_level("WARNING"):
        assert nc.load_news_context(cfg) == ""
    assert any("s3_bucket is unset" in r.message for r in caplog.records)


# ── happy path ───────────────────────────────────────────────────────────────


@mock_aws
def test_happy_path_formats_sections(aws_env):
    _seed_digest("news-bucket", nc.DEFAULT_S3_KEY, _SAMPLE_DIGEST)
    cfg = {
        "s3_region": REGION,
        "news_context": {"enabled": True, "s3_bucket": "news-bucket"},
    }
    out = nc.load_news_context(cfg)

    # Header frames the digest as SUPPLEMENTARY and mandates web search —
    # it must NOT tell the model to skip searching (the 2026-06-16 regression
    # that produced a zero-search hallucinated episode).
    assert out.startswith("PRE-FETCHED NEWS")
    assert "SUPPLEMENTARY" in out
    assert "MUST still run web search" in out
    assert "do NOT web-search" not in out
    # The header explicitly flags that political segments are absent + must be searched.
    assert "political" in out.lower()
    # Generic section headings (titlecased from keys)
    assert "## Portfolio" in out
    assert "## Macro" in out
    assert "## Tech" in out
    # Item rendering: ticker prefix + (source, published) + excerpt
    assert "- [AAPL] Apple unveils new chip (Reuters, 2026-06-14) — Faster, cooler." in out
    # No-ticker, no-published item renders cleanly (source only attribution)
    assert "- New model released (TechCrunch) — Big leap." in out
    # Footer reinforces: supplementary leads only, search every segment.
    assert "supplementary leads only" in out


@mock_aws
def test_uses_custom_s3_key(aws_env):
    _seed_digest("news-bucket", "custom/digest.json", _SAMPLE_DIGEST)
    cfg = {
        "s3_region": REGION,
        "news_context": {
            "enabled": True,
            "s3_bucket": "news-bucket",
            "s3_key": "custom/digest.json",
        },
    }
    out = nc.load_news_context(cfg)
    assert "## Portfolio" in out


@mock_aws
def test_empty_sections_skipped(aws_env):
    digest = {"sections": {"portfolio": [], "macro": [{"title": "Only macro"}]}}
    _seed_digest("news-bucket", nc.DEFAULT_S3_KEY, digest)
    cfg = {
        "s3_region": REGION,
        "news_context": {"enabled": True, "s3_bucket": "news-bucket"},
    }
    out = nc.load_news_context(cfg)
    assert "## Portfolio" not in out
    assert "## Macro" in out


@mock_aws
def test_all_empty_sections_returns_empty(aws_env):
    digest = {"sections": {"portfolio": [], "macro": []}}
    _seed_digest("news-bucket", nc.DEFAULT_S3_KEY, digest)
    cfg = {
        "s3_region": REGION,
        "news_context": {"enabled": True, "s3_bucket": "news-bucket", "required": False},
    }
    assert nc.load_news_context(cfg) == ""


# ── fail-soft ────────────────────────────────────────────────────────────────


@mock_aws
def test_s3_miss_fails_soft(aws_env, caplog):
    # Bucket exists but the digest object does not.
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(
        Bucket="news-bucket",
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    cfg = {
        "s3_region": REGION,
        "news_context": {"enabled": True, "s3_bucket": "news-bucket", "required": False},
    }
    with caplog.at_level("WARNING"):
        assert nc.load_news_context(cfg) == ""
    assert any("could not load digest" in r.message for r in caplog.records)


@mock_aws
def test_bad_json_fails_soft(aws_env, caplog):
    _seed_digest("news-bucket", nc.DEFAULT_S3_KEY, "{not valid json")
    cfg = {
        "s3_region": REGION,
        "news_context": {"enabled": True, "s3_bucket": "news-bucket", "required": False},
    }
    with caplog.at_level("WARNING"):
        assert nc.load_news_context(cfg) == ""
    assert any("could not load digest" in r.message for r in caplog.records)


@mock_aws
def test_missing_sections_key_fails_soft(aws_env, caplog):
    _seed_digest("news-bucket", nc.DEFAULT_S3_KEY, {"schema_version": 1})
    cfg = {
        "s3_region": REGION,
        "news_context": {"enabled": True, "s3_bucket": "news-bucket", "required": False},
    }
    with caplog.at_level("WARNING"):
        assert nc.load_news_context(cfg) == ""
    assert any("no usable 'sections'" in r.message for r in caplog.records)


# ── required (default) → fail-loud ────────────────────────────────────────────

_FRESH_DIGEST = {
    "schema_version": 1,
    "date": "2026-06-16",
    "sections": {"portfolio": [{"ticker": "AAPL", "title": "Apple news"}]},
}


def test_required_defaults_true_and_raises_without_bucket():
    """Enabled with no ``required`` key defaults to strict — a config gap
    (no bucket) raises rather than silently degrading."""
    cfg = {"news_context": {"enabled": True}}
    with pytest.raises(RuntimeError, match="news_context required but"):
        nc.load_news_context(cfg)


@mock_aws
def test_required_raises_on_s3_miss(aws_env):
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(
        Bucket="news-bucket",
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    cfg = {
        "s3_region": REGION,
        "news_context": {"enabled": True, "s3_bucket": "news-bucket"},
    }
    with pytest.raises(RuntimeError, match="could not load digest"):
        nc.load_news_context(cfg, run_date="2026-06-16")


@mock_aws
def test_required_raises_on_empty_digest(aws_env):
    digest = {"date": "2026-06-16", "sections": {"portfolio": [], "macro": [], "tech": []}}
    _seed_digest("news-bucket", nc.DEFAULT_S3_KEY, digest)
    cfg = {
        "s3_region": REGION,
        "news_context": {"enabled": True, "s3_bucket": "news-bucket"},
    }
    with pytest.raises(RuntimeError, match="has no usable items"):
        nc.load_news_context(cfg, run_date="2026-06-16")


@mock_aws
def test_required_raises_on_stale_digest(aws_env):
    # Digest is dated 2026-06-16; the run is for 2026-06-17 → stale.
    _seed_digest("news-bucket", nc.DEFAULT_S3_KEY, _FRESH_DIGEST)
    cfg = {
        "s3_region": REGION,
        "news_context": {"enabled": True, "s3_bucket": "news-bucket"},
    }
    with pytest.raises(RuntimeError, match="stale"):
        nc.load_news_context(cfg, run_date="2026-06-17")


@mock_aws
def test_required_passes_on_fresh_nonempty_digest(aws_env):
    _seed_digest("news-bucket", nc.DEFAULT_S3_KEY, _FRESH_DIGEST)
    cfg = {
        "s3_region": REGION,
        "news_context": {"enabled": True, "s3_bucket": "news-bucket"},
    }
    out = nc.load_news_context(cfg, run_date="2026-06-16")
    assert "## Portfolio" in out
    assert "Apple news" in out


@mock_aws
def test_no_run_date_skips_staleness_check(aws_env):
    """Without a run_date the staleness gate is skipped, but presence +
    non-empty are still required."""
    _seed_digest("news-bucket", nc.DEFAULT_S3_KEY, _FRESH_DIGEST)
    cfg = {
        "s3_region": REGION,
        "news_context": {"enabled": True, "s3_bucket": "news-bucket"},
    }
    out = nc.load_news_context(cfg)  # no run_date
    assert "## Portfolio" in out


# ── generate_script injection wiring ─────────────────────────────────────────


def _make_anthropic_mock(text: str = "Body."):
    from unittest.mock import MagicMock

    block = MagicMock()
    block.type = "text"
    block.text = text
    usage = MagicMock()
    usage.input_tokens = 100
    usage.output_tokens = 200
    usage.cache_read_input_tokens = None
    usage.cache_creation_input_tokens = None
    usage.server_tool_use = None
    response = MagicMock()
    response.content = [block]
    response.model = "claude-sonnet-4-6"
    response.usage = usage
    client_inst = MagicMock()
    client_inst.messages.create.return_value = response
    anthropic_module = MagicMock()
    anthropic_module.Anthropic.return_value = client_inst
    return anthropic_module, client_inst


def test_generate_script_injects_news_block_when_enabled(fresh_ge_module, tmp_path):
    prompt_path = tmp_path / "p.md"
    prompt_path.write_text("# prompt")

    anth_mock, client = _make_anthropic_mock("Episode body.")
    fake_block = (
        "PRE-FETCHED NEWS (use the items below for these topics; do NOT "
        "web-search them):\n\n## Portfolio\n- [AAPL] Apple ships chip"
    )
    with patch.dict(sys.modules, {"anthropic": anth_mock}), patch.object(
        _config, "PROMPT_FILE", prompt_path
    ), patch.object(_claude, "load_news_context", return_value=fake_block):
        fresh_ge_module.generate_script(
            {"claude_model": "x", "max_tokens": 1, "min_web_searches": 0}, "2026-05-14", "am"
        )

    _, kwargs = client.messages.create.call_args
    user_content = kwargs["messages"][0]["content"]
    # The block is present...
    assert "PRE-FETCHED NEWS" in user_content
    assert "[AAPL] Apple ships chip" in user_content
    # ...positioned AFTER the edition sentence and BEFORE the generate-instruction...
    edition_idx = user_content.index("This is the MORNING edition")
    block_idx = user_content.index("PRE-FETCHED NEWS")
    gen_idx = user_content.index("Generate today's")
    assert edition_idx < block_idx < gen_idx
    # ...and the canonical-opener instruction stays at the END, unchanged.
    assert user_content.rstrip().endswith("Welcome to Morning Signal.")
    assert "MUST begin verbatim" in user_content


def test_generate_script_no_injection_when_context_empty(fresh_ge_module, tmp_path):
    """Default behavior (feature off → "") leaves the user message exactly
    as before: no news header, generate-instruction immediately follows the
    edition sentence."""
    prompt_path = tmp_path / "p.md"
    prompt_path.write_text("# prompt")

    anth_mock, client = _make_anthropic_mock("Episode body.")
    with patch.dict(sys.modules, {"anthropic": anth_mock}), patch.object(
        _config, "PROMPT_FILE", prompt_path
    ), patch.object(_claude, "load_news_context", return_value=""):
        fresh_ge_module.generate_script(
            {"claude_model": "x", "max_tokens": 1, "min_web_searches": 0}, "2026-05-14", "am"
        )

    _, kwargs = client.messages.create.call_args
    user_content = kwargs["messages"][0]["content"]
    assert "PRE-FETCHED NEWS" not in user_content
    assert "This is the MORNING edition of Morning Signal.\n\nGenerate today's" in user_content
