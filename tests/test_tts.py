"""Tests for the TTS engine seam — synthesize() dispatch + tts_google()."""

from __future__ import annotations

import subprocess

import pytest

from morning_signal import tts


def test_synthesize_dispatches_to_polly_by_default(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(tts, "tts_polly", lambda s, o, c: calls.append(("polly", s, o)))
    monkeypatch.setattr(tts, "tts_google", lambda s, o, c: calls.append(("google", s, o)))

    out = tmp_path / "ep.mp3"
    tts.synthesize("hello", out, {"tts": {}})  # no engine key → polly

    assert calls == [("polly", "hello", out)]


def test_synthesize_dispatches_to_google_when_configured(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(tts, "tts_polly", lambda s, o, c: calls.append("polly"))
    monkeypatch.setattr(tts, "tts_google", lambda s, o, c: calls.append("google"))

    tts.synthesize("hi", tmp_path / "ep.mp3", {"tts": {"engine": "google"}})

    assert calls == ["google"]


def test_synthesize_engine_is_case_insensitive(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(tts, "tts_google", lambda s, o, c: seen.append("google"))
    tts.synthesize("hi", tmp_path / "ep.mp3", {"tts": {"engine": "GOOGLE"}})
    assert seen == ["google"]


def test_synthesize_unknown_engine_raises(tmp_path):
    with pytest.raises(ValueError, match="Unknown tts.engine='espeak'"):
        tts.synthesize("hi", tmp_path / "ep.mp3", {"tts": {"engine": "espeak"}})


def test_tts_google_chunks_writes_and_uses_config_voice(monkeypatch, tmp_path):
    """tts_google should chunk on the byte limit, concat, and honor the
    configured voice/language — all without a real API call."""
    gtts = pytest.importorskip("google.cloud.texttospeech")  # skip if [google] extra absent

    captured = {}

    class _FakeResp:
        audio_content = b"ID3-fake-mp3-bytes"

    class _FakeClient:
        def synthesize_speech(self, *, input, voice, audio_config):
            captured["voice"] = voice.name
            captured["language"] = voice.language_code
            captured.setdefault("chunks", []).append(input.text)
            return _FakeResp()

    monkeypatch.setattr(gtts, "TextToSpeechClient", lambda *a, **k: _FakeClient())

    # Two sentences long enough to force >1 chunk at the 4500 limit.
    long_sentence = ("word " * 1000).strip() + "."
    script = f"{long_sentence} {long_sentence}"

    out = tmp_path / "ep.mp3"
    tts.tts_google(script, out, {"tts": {"engine": "google", "google_voice": "en-US-Chirp3-HD-Leda", "speed": 1.0}})

    assert out.exists() and out.read_bytes()  # concatenated output written
    assert captured["voice"] == "en-US-Chirp3-HD-Leda"
    assert captured["language"] == "en-US"
    assert len(captured["chunks"]) >= 2  # chunking actually happened
    # temp chunk files are cleaned up
    assert not list(tmp_path.glob("_gchunk_*.mp3"))


def _max_sentence_len(chunk: str) -> int:
    import re

    return max((len(s) for s in re.split(r"(?<=[.!?])\s+", chunk)), default=0)


def test_chunk_text_splits_oversized_sentence_at_clause_boundaries():
    """Regression for 2026-06-22 AM: a 570-char run-on sentence (the NVIDIA bond
    offering) tripped Chirp3 HD's per-sentence cap. With max_sentence_len set, no
    emitted chunk may contain a sentence longer than the cap."""
    cap = 400
    run_on = (
        "On June 18, 2026, NVIDIA Corporation completed an offering of "
        "$3,500,000,000 aggregate principal amount of senior notes, "
        + ("comprising several tranches with staggered maturities, ") * 12
        + "with proceeds earmarked for general corporate purposes."
    )
    assert len(run_on) > cap  # genuinely oversized input
    script = f"A short lead sentence. {run_on} A short trailing sentence."

    chunks = tts._chunk_text(script, 4500, max_sentence_len=cap)

    assert chunks
    for chunk in chunks:
        assert _max_sentence_len(chunk) <= cap


def test_chunk_text_leaves_normal_sentences_intact():
    """Sentences within the cap are not mangled — count is preserved."""
    script = "First sentence here. Second one follows. Third wraps it up."
    chunks = tts._chunk_text(script, 4500, max_sentence_len=400)
    assert " ".join(chunks).split() == script.split()


def test_split_oversized_sentence_terminates_each_fragment():
    long = "alpha beta gamma delta epsilon zeta eta theta " * 20  # no internal punctuation
    frags = tts._split_oversized_sentence(long.strip(), 200)
    assert len(frags) > 1
    assert all(f[-1] in ".!?" for f in frags)
    assert all(len(f) <= 200 for f in frags)


def test_adjust_speed_retries_then_succeeds(monkeypatch, tmp_path):
    """A transient ffmpeg abort (SIGABRT under memory pressure) should be ridden
    out by the bounded retry rather than killing the episode."""
    path = tmp_path / "ep.mp3"
    path.write_bytes(b"original")
    calls = {"n": 0}

    def fake_run(cmd, **kwargs):
        calls["n"] += 1
        tmp = path.with_suffix(".tmp.mp3")
        if calls["n"] < 2:  # first attempt aborts
            tmp.write_bytes(b"partial")  # ffmpeg may leave a stub behind
            raise subprocess.CalledProcessError(-6, cmd, stderr=b"Aborted (core dumped)")
        tmp.write_bytes(b"sped-up")  # second attempt succeeds
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(tts.subprocess, "run", fake_run)
    monkeypatch.setattr(tts.time, "sleep", lambda s: None)  # no real backoff

    tts._adjust_speed(path, 1.5)

    assert calls["n"] == 2
    assert path.read_bytes() == b"sped-up"
    assert not list(tmp_path.glob("*.tmp.mp3"))  # failed-attempt stub cleaned up


def test_adjust_speed_raises_after_exhausting_retries(monkeypatch, tmp_path, caplog):
    """On persistent failure, raise loud and surface ffmpeg's stderr (which
    check=True would otherwise swallow)."""
    path = tmp_path / "ep.mp3"
    path.write_bytes(b"original")

    def always_abort(cmd, **kwargs):
        raise subprocess.CalledProcessError(-6, cmd, stderr=b"out of memory\nAborted")

    monkeypatch.setattr(tts.subprocess, "run", always_abort)
    monkeypatch.setattr(tts.time, "sleep", lambda s: None)

    with caplog.at_level("ERROR"), pytest.raises(subprocess.CalledProcessError):
        tts._adjust_speed(path, 1.5, attempts=3)

    assert "out of memory" in caplog.text  # stderr surfaced, not swallowed
    assert not list(tmp_path.glob("*.tmp.mp3"))


def test_tts_google_missing_dep_raises_with_install_hint(monkeypatch, tmp_path):
    """If the google extra isn't installed, fail loud with the install hint."""
    import builtins

    real_import = builtins.__import__

    def _blocked(name, *args, **kwargs):
        if name == "google.cloud" or name.startswith("google.cloud"):
            raise ImportError("No module named 'google.cloud.texttospeech'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked)

    with pytest.raises(ImportError, match=r"morning-signal\[google\]"):
        tts.tts_google("hi", tmp_path / "ep.mp3", {"tts": {"engine": "google"}})
