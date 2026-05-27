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


def test_load_prompt_weekend_reads_separate_file(fresh_ge_module, tmp_path):
    weekday = tmp_path / "p.md"
    weekend = tmp_path / "p_weekend.md"
    weekday.write_text("weekday body")
    weekend.write_text("  weekend body  \n")
    with patch.object(_config, "PROMPT_FILE", weekday), \
         patch.object(_config, "PROMPT_WEEKEND_FILE", weekend):
        assert fresh_ge_module.load_prompt(weekend=False) == "weekday body"
        assert fresh_ge_module.load_prompt(weekend=True) == "weekend body"


def test_load_prompt_public_mode_reads_separate_file(fresh_ge_module, tmp_path):
    # public_mode overrides weekend — prompt_public.md handles its own
    # MORNING / EVENING / WEEKEND openers + news-windows internally.
    weekday = tmp_path / "p.md"
    weekend = tmp_path / "p_weekend.md"
    public = tmp_path / "p_public.md"
    weekday.write_text("personal weekday")
    weekend.write_text("personal weekend")
    public.write_text("  public catalog  \n")
    with patch.object(_config, "PROMPT_FILE", weekday), \
         patch.object(_config, "PROMPT_WEEKEND_FILE", weekend), \
         patch.object(_config, "PROMPT_PUBLIC_FILE", public):
        assert fresh_ge_module.load_prompt(public_mode=True) == "public catalog"
        # public_mode wins over weekend flag
        assert fresh_ge_module.load_prompt(
            weekend=True, public_mode=True,
        ) == "public catalog"
        # without public_mode the prior dispatch is preserved
        assert fresh_ge_module.load_prompt(weekend=False) == "personal weekday"
        assert fresh_ge_module.load_prompt(weekend=True) == "personal weekend"


def test_load_prompt_public_mode_exits_when_missing(fresh_ge_module, tmp_path):
    # public_topics_mode flipped on but prompt_public.md not synced to
    # SSM yet → fail loud at episode generation time, do not silently
    # fall back to the personal prompt.
    weekday = tmp_path / "p.md"
    weekday.write_text("body")
    with patch.object(_config, "PROMPT_FILE", weekday), \
         patch.object(_config, "PROMPT_PUBLIC_FILE", tmp_path / "no_public.md"):
        try:
            fresh_ge_module.load_prompt(public_mode=True)
        except SystemExit as e:
            assert e.code == 1
        else:
            raise AssertionError("expected SystemExit")


def test_load_prompt_weekend_exits_when_missing(fresh_ge_module, tmp_path):
    weekday = tmp_path / "p.md"
    weekday.write_text("body")
    with patch.object(_config, "PROMPT_FILE", weekday), \
         patch.object(_config, "PROMPT_WEEKEND_FILE", tmp_path / "no_weekend.md"):
        try:
            fresh_ge_module.load_prompt(weekend=True)
        except SystemExit as e:
            assert e.code == 1
        else:
            raise AssertionError("expected SystemExit")


def test_edition_labels_complete(fresh_ge_module):
    assert fresh_ge_module.EDITION_LABELS["am"] == "MORNING"
    assert fresh_ge_module.EDITION_LABELS["pm"] == "EVENING"
