"""Generation and verified delivery are separate checkpoints (alpha-engine-config-I12901).

A failure at any publish cut point (MP3 upload, feed upload, verification)
leaves the episode `generated`; the next ordinary run resumes publication
from the existing audio with no LLM or TTS call. Only a verified feed
delivery dedups. The watchdog treats a fresh MP3 behind a feed that lacks
the episode as unhealthy.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from morning_signal import config as _config
from morning_signal import publish as _publish
from morning_signal import watchdog

REGION = "us-west-2"
BUCKET = "test-bucket"
DATE, EDITION = "2026-05-14", "am"
STEM = f"{DATE}-{EDITION}"


@pytest.fixture
def env(monkeypatch, tmp_path, fresh_ge_module, sample_config):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(json.dumps(sample_config))
    monkeypatch.setattr(_config, "CONFIG_FILE", cfg)
    monkeypatch.setattr(_config, "FEED_FILE", tmp_path / "feed.xml")
    monkeypatch.chdir(tmp_path)  # no artwork in cwd

    calls: list[str] = []

    def fake_script(*a, **kw):
        calls.append("llm")
        return "Today's briefing."

    def fake_tts(script, audio_path, config):
        calls.append("tts")
        Path(audio_path).write_bytes(b"\xff\xfb" * 512)

    monkeypatch.setattr(fresh_ge_module, "generate_script", fake_script)
    monkeypatch.setattr(fresh_ge_module, "synthesize", fake_tts)
    return fresh_ge_module, sample_config, calls


def _bucket():
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
    return s3


def _run(ge, monkeypatch, *flags):
    monkeypatch.setattr(sys, "argv", ["generate_episode.py", "--date", DATE, "--edition", EDITION, *flags])
    ge.main()


def _meta(ge):
    return json.loads((_config.EPISODES_DIR / f"{STEM}.json").read_text())


def _mp3_upload_fails(*a, **kw):
    raise RuntimeError("S3 PutObject failed")


def _mp3_uploads_then_feed_fails(config, fresh_uploads=None):
    s3 = boto3.client("s3", region_name=REGION)
    for name in fresh_uploads or ():
        s3.upload_file(str(_config.EPISODES_DIR / name), BUCKET, f"episodes/{name}")
    raise RuntimeError("feed.xml upload failed")


def _publishes_nothing(config, fresh_uploads=None):
    return None  # "succeeds" but the feed never moves: verification must catch it


@mock_aws
@pytest.mark.parametrize("cut", [_mp3_upload_fails, _mp3_uploads_then_feed_fails, _publishes_nothing])
def test_failed_delivery_resumes_on_the_next_ordinary_run_without_llm_or_tts(env, monkeypatch, cut):
    ge, config, calls = env
    _bucket()
    monkeypatch.setattr(ge, "publish_to_s3", cut)
    with pytest.raises(Exception):
        _run(ge, monkeypatch)
    assert calls == ["llm", "tts"]
    assert _meta(ge)["published_at"] is None
    assert ge._episode_state(DATE, EDITION) == "generated"
    assert ge._existing_episode(DATE, EDITION) is False

    # Ordinary retry: real publish, no regeneration.
    monkeypatch.setattr(ge, "publish_to_s3", _publish.publish_to_s3)
    _run(ge, monkeypatch)
    assert calls == ["llm", "tts"]
    meta = _meta(ge)
    assert meta["published_at"] and meta["published_enclosure"].endswith(f"/episodes/{STEM}.mp3")
    watchdog.check_feed_delivers(config, DATE, EDITION)

    # Fully verified delivery dedups: no publish attempt at all.
    monkeypatch.setattr(ge, "publish_to_s3", lambda *a, **kw: calls.append("publish"))
    _run(ge, monkeypatch)
    assert calls == ["llm", "tts"]


@mock_aws
def test_no_publish_then_ordinary_run_publishes_the_existing_audio(env, monkeypatch):
    ge, _config_dict, calls = env
    _bucket()
    _run(ge, monkeypatch, "--no-publish")
    assert calls == ["llm", "tts"] and _meta(ge)["published_at"] is None
    _run(ge, monkeypatch, "--no-publish")  # nothing to do; no regeneration
    assert calls == ["llm", "tts"]
    _run(ge, monkeypatch)
    assert calls == ["llm", "tts"] and _meta(ge)["published_at"]


@mock_aws
def test_script_only_still_writes_no_audio_and_publishes_nothing(env, monkeypatch):
    ge, _c, calls = env
    _bucket()
    monkeypatch.setattr(ge, "publish_to_s3", lambda *a, **kw: calls.append("publish"))
    _run(ge, monkeypatch, "--script-only")
    assert calls == ["llm"] and _meta(ge)["audio_file"] is None


@mock_aws
def test_publish_only_verifies_and_checkpoints_a_pending_episode(env, monkeypatch):
    ge, _c, calls = env
    _bucket()
    _run(ge, monkeypatch, "--no-publish")
    _run(ge, monkeypatch, "--publish-only")
    assert calls == ["llm", "tts"] and _meta(ge)["published_at"]


def test_legacy_metadata_without_the_field_counts_as_published(fresh_ge_module, make_episode):
    make_episode(DATE, EDITION)
    assert fresh_ge_module._episode_state(DATE, EDITION) == "published"


def test_pending_episode_whose_audio_is_gone_is_absent(fresh_ge_module, tmp_episodes_dir, tmp_scripts_dir):
    fresh_ge_module.save_metadata(DATE, EDITION, tmp_scripts_dir / "s.md", tmp_episodes_dir / "gone.mp3")
    assert fresh_ge_module._episode_state(DATE, EDITION) == "absent"


def test_interrupted_metadata_write_keeps_the_previous_file(fresh_ge_module, tmp_episodes_dir, monkeypatch):
    path = tmp_episodes_dir / f"{STEM}.json"
    path.write_text(json.dumps({"audio_file": "/x.mp3", "published_at": None}))

    def crash(*a):
        raise KeyboardInterrupt

    monkeypatch.setattr("morning_signal.episode.os.replace", crash)
    with pytest.raises(KeyboardInterrupt):
        fresh_ge_module._write_meta(path, {"audio_file": "/x.mp3", "published_at": "now"})
    assert json.loads(path.read_text())["published_at"] is None


# --- watchdog: the feed, not just the MP3 ------------------------------------

def _feed(*enclosures: str) -> bytes:
    items = "".join(
        f'<item><title>t</title><enclosure url="{u}" length="1" type="audio/mpeg"/></item>'
        for u in enclosures
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel>{items}</channel></rss>'.encode()


WD_CONFIG = {"s3_bucket": BUCKET, "s3_region": REGION, "s3_prefix": ""}
URL = f"https://{BUCKET}.s3.{REGION}.amazonaws.com/episodes/{STEM}.mp3"


@pytest.fixture
def aws(monkeypatch):
    from morning_signal import aws as _aws
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(k, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setattr(_aws, "_AWS_SESSION", None)


@mock_aws
@pytest.mark.parametrize("feed, mp3, needle", [
    (None, b"x" * 64, "not found"),
    (b"<rss><channel><item>", b"x" * 64, "well-formed"),
    (_feed(URL.replace(STEM, "2026-05-13-am")), b"x" * 64, "no item"),
    (_feed(URL), b"", "empty"),
    (_feed(URL), None, "not found"),
])
def test_fresh_mp3_with_a_bad_feed_is_unhealthy(aws, feed, mp3, needle):
    s3 = _bucket()
    if mp3 is not None:
        s3.put_object(Bucket=BUCKET, Key=f"episodes/{STEM}.mp3", Body=mp3)
    if feed is not None:
        s3.put_object(Bucket=BUCKET, Key="feed.xml", Body=feed)
    with pytest.raises(watchdog.FeedUndelivered, match=needle):
        watchdog.check_feed_delivers(WD_CONFIG, DATE, EDITION)


@mock_aws
def test_feed_carrying_the_episode_is_healthy(aws):
    s3 = _bucket()
    s3.put_object(Bucket=BUCKET, Key=f"episodes/{STEM}.mp3", Body=b"x" * 64)
    s3.put_object(Bucket=BUCKET, Key="feed.xml", Body=_feed(URL))
    assert watchdog.check_feed_delivers(WD_CONFIG, DATE, EDITION) == URL


def test_corrupt_metadata_is_absent_and_mark_published_ignores_missing(fresh_ge_module, tmp_episodes_dir):
    (tmp_episodes_dir / f"{STEM}.json").write_text("{not json")
    assert fresh_ge_module._episode_state(DATE, EDITION) == "absent"
    fresh_ge_module.mark_published("2099-01-01", "am", "u")  # no metadata: no-op
    assert not (tmp_episodes_dir / "2099-01-01-am.json").exists()


class _Denied(Exception):
    response = {"Error": {"Code": "AccessDenied"}}


class _Client:
    def __init__(self, fail: str):
        self.fail = fail

    def get_object(self, **kw):
        if self.fail == "get":
            raise _Denied("denied")
        return {"Body": type("B", (), {"read": lambda s: _feed(URL)})()}

    def head_object(self, **kw):
        raise _Denied("denied")


@pytest.mark.parametrize("fail", ["get", "head"])
def test_s3_errors_other_than_missing_propagate(monkeypatch, fail):
    from morning_signal import aws as _aws
    monkeypatch.setattr(_aws, "_aws_client", lambda *a, **kw: _Client(fail))
    with pytest.raises(_Denied):
        watchdog.check_feed_delivers(WD_CONFIG, DATE, EDITION)


@mock_aws
def test_prefixed_feed_and_enclosure_are_checked_under_the_prefix(aws):
    s3 = _bucket()
    s3.put_object(Bucket=BUCKET, Key=f"pod/episodes/{STEM}.mp3", Body=b"x" * 64)
    url = URL.replace("/episodes/", "/pod/episodes/")
    s3.put_object(Bucket=BUCKET, Key="pod/feed.xml", Body=_feed(url))
    cfg = dict(WD_CONFIG, s3_prefix="pod/")
    assert watchdog.feed_key(cfg) == "pod/feed.xml"
    assert watchdog.check_feed_delivers(cfg, DATE, EDITION) == url


@pytest.mark.parametrize("target, sent", [("telegram:1", True), (None, False)])
def test_send_alert_reports_whether_the_notifier_sent(monkeypatch, target, sent):
    from morning_signal import notify

    class _N:
        def send_raw(self, message):
            return target

    monkeypatch.setattr(notify, "make_doctor", lambda config, edition: (None, _N()))
    assert watchdog.send_alert({}, "am", "m") is sent
