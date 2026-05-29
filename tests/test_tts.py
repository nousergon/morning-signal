"""Tests for the TTS engine seam — synthesize() dispatch + tts_google()."""

from __future__ import annotations

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


def test_synthesize_segments_renders_each_and_concats(monkeypatch, tmp_path):
    calls = []

    def fake_synth(text, out, config):
        calls.append(text)
        out.write_bytes(b"MP3:" + text.encode())

    monkeypatch.setattr(tts, "synthesize", fake_synth)
    out = tmp_path / "ep.mp3"
    tts.synthesize_segments(["intro", "topic A", "topic B"], out, {"tts": {}})

    assert calls == ["intro", "topic A", "topic B"]
    assert out.exists()
    data = out.read_bytes()
    assert b"intro" in data and b"topic A" in data and b"topic B" in data
    assert not list(tmp_path.glob("_seg_*.mp3"))  # temps cleaned up


def test_synthesize_segments_single_script_renames(monkeypatch, tmp_path):
    monkeypatch.setattr(tts, "synthesize", lambda t, o, c: o.write_bytes(b"X"))
    out = tmp_path / "ep.mp3"
    tts.synthesize_segments(["only one"], out, {"tts": {}})
    assert out.read_bytes() == b"X"
    assert not list(tmp_path.glob("_seg_*.mp3"))


def test_synthesize_segments_empty_raises(tmp_path):
    with pytest.raises(ValueError, match="no scripts"):
        tts.synthesize_segments([], tmp_path / "ep.mp3", {"tts": {}})


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
