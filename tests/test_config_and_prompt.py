"""Tests for config + prompt loading."""

from __future__ import annotations

from unittest.mock import patch

from morning_signal import config as _config


def test_load_config_reads_yaml(fresh_ge_module, tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("s3_bucket: foo\nclaude_model: bar\n")
    with patch.object(_config, "CONFIG_FILE", cfg):
        result = fresh_ge_module.load_config()
    assert result == {"s3_bucket": "foo", "claude_model": "bar"}


def test_load_config_exits_when_missing(fresh_ge_module, tmp_path):
    with patch.object(_config, "CONFIG_FILE", tmp_path / "no.yaml"):
        try:
            fresh_ge_module.load_config()
        except SystemExit as e:
            assert e.code == 1
        else:
            raise AssertionError("expected SystemExit")


def test_load_prompt_reads_and_strips(fresh_ge_module, tmp_path):
    p = tmp_path / "p.md"
    p.write_text("  hello prompt  \n\n")
    with patch.object(_config, "PROMPT_FILE", p):
        assert fresh_ge_module.load_prompt() == "hello prompt"


def test_load_prompt_exits_when_missing(fresh_ge_module, tmp_path):
    with patch.object(_config, "PROMPT_FILE", tmp_path / "no.md"):
        try:
            fresh_ge_module.load_prompt()
        except SystemExit as e:
            assert e.code == 1
        else:
            raise AssertionError("expected SystemExit")


def test_edition_labels_complete(fresh_ge_module):
    assert fresh_ge_module.EDITION_LABELS["am"] == "MORNING"
    assert fresh_ge_module.EDITION_LABELS["pm"] == "EVENING"
