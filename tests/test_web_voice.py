from __future__ import annotations

import io
from pathlib import Path

import pytest

from usr.libexec.web.pai_web import actions, voice
from usr.libexec.web.pai_web.server import Handler


# --- dispatcher selection --------------------------------------------------


def test_dispatcher_prefers_local_then_cloud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        voice,
        "_discover_packages",
        lambda: [
            {"name": "voice_cloud", "provides": ["stt", "tts"], "voice_mode": "cloud"},
            {"name": "voice", "provides": ["stt", "tts"], "voice_mode": "local"},
        ],
    )
    monkeypatch.setattr(voice, "_voice_config", lambda: {})
    assert voice._candidates("tts") == ["voice", "voice_cloud"]
    assert voice._candidates("stt") == ["voice", "voice_cloud"]


def test_dispatcher_config_pin_overrides_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        voice,
        "_discover_packages",
        lambda: [
            {"name": "voice", "provides": ["stt", "tts"], "voice_mode": "local"},
            {"name": "voice_cloud", "provides": ["stt", "tts"], "voice_mode": "cloud"},
        ],
    )
    # Pin only stt to cloud; tts keeps the local default.
    monkeypatch.setattr(voice, "_voice_config", lambda: {"stt": "voice_cloud"})
    assert voice._candidates("stt")[0] == "voice_cloud"
    assert voice._candidates("tts")[0] == "voice"


def test_dispatcher_skips_provider_whose_deps_fail_to_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        voice,
        "_discover_packages",
        lambda: [
            {"name": "voice", "provides": ["stt"], "voice_mode": "local"},
            {"name": "voice_cloud", "provides": ["stt"], "voice_mode": "cloud"},
        ],
    )
    monkeypatch.setattr(voice, "_voice_config", lambda: {})

    class CloudProvider:
        @staticmethod
        def transcribe(*a, **k):  # noqa: ANN002, ANN003
            return "cloud"

    # Local deps missing (ImportError → None); cloud imports fine → cloud wins.
    monkeypatch.setattr(
        voice,
        "_import_provider",
        lambda name: None if name == "voice" else CloudProvider,
    )
    assert voice.resolve_provider("stt") is CloudProvider


# --- actions delegation ----------------------------------------------------


def test_synthesize_speech_delegates_to_resolved_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    class Provider:
        @staticmethod
        def synthesize(text: str, *, voice_id, speed) -> tuple[bytes, str]:  # noqa: ANN001
            captured.update(text=text, voice_id=voice_id, speed=speed)
            return b"prov-bytes", "audio/mpeg"

    monkeypatch.setattr(
        actions.voice, "resolve_provider", lambda cap: Provider if cap == "tts" else None
    )
    audio = actions.synthesize_speech("hello", voice_id="voice-test", speed=1.1)

    assert audio == actions.SpeechAudio(b"prov-bytes", "audio/mpeg")
    assert captured == {"text": "hello", "voice_id": "voice-test", "speed": 1.1}


def test_synthesize_speech_falls_back_to_macos_say_without_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs: list[dict] = []

    def fake_run(args, input, text, capture_output, check, timeout):  # noqa: A002
        runs.append({"args": args, "input": input, "timeout": timeout})
        if args[0] == "/usr/bin/say":
            Path(args[2]).write_bytes(b"aiff-bytes")
        elif args[0] == "/usr/bin/afconvert":
            Path(args[-1]).write_bytes(b"m4a-bytes")
        return actions.subprocess.CompletedProcess(args, 0)

    # No installed provider → last-resort macOS `say`.
    monkeypatch.setattr(actions.voice, "resolve_provider", lambda cap: None)
    monkeypatch.setattr(
        actions.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in {"say", "afconvert"} else None,
    )
    monkeypatch.setattr(actions.subprocess, "run", fake_run)

    audio = actions.synthesize_speech("hello from fallback", voice_id="ignored", speed=0.8)

    assert audio == actions.SpeechAudio(b"m4a-bytes", "audio/mp4")
    assert len(runs) == 2
    assert runs[0]["args"][0] == "/usr/bin/say"
    assert runs[0]["args"][3:] == ["-f", "-"]
    assert runs[0]["input"] == "hello from fallback"
    assert runs[1]["args"][0] == "/usr/bin/afconvert"


def test_transcribe_speech_delegates_to_resolved_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    class Provider:
        @staticmethod
        def transcribe(audio, *, content_type, filename, language, prompt) -> str:  # noqa: ANN001
            captured.update(
                audio=audio,
                content_type=content_type,
                filename=filename,
                language=language,
                prompt=prompt,
            )
            return "hello from provider"

    monkeypatch.setattr(
        actions.voice, "resolve_provider", lambda cap: Provider if cap == "stt" else None
    )
    text = actions.transcribe_speech(
        b"audio-bytes", filename="clip.webm", content_type="audio/webm", language="en"
    )

    assert text == "hello from provider"
    assert captured["audio"] == b"audio-bytes"
    assert captured["filename"] == "clip.webm"
    assert captured["content_type"] == "audio/webm"
    assert captured["language"] == "en"


def test_transcribe_speech_requires_a_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(actions.voice, "resolve_provider", lambda cap: None)
    with pytest.raises(RuntimeError, match="no speech-to-text provider"):
        actions.transcribe_speech(
            b"audio", filename="clip.webm", content_type="audio/webm"
        )


# --- server routing (engine-agnostic) --------------------------------------


def test_tts_route_uses_synthesized_content_type(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_synthesize(text: str, *, voice_id: str | None, speed: float | None) -> actions.SpeechAudio:
        captured["text"] = text
        captured["voice_id"] = voice_id
        captured["speed"] = speed
        return actions.SpeechAudio(b"m4a-route-bytes", "audio/mp4")

    handler = Handler.__new__(Handler)
    handler._binary = lambda data, content_type, status=200: captured.update(
        data=data,
        content_type=content_type,
        status=status,
    )
    monkeypatch.setattr(actions, "synthesize_speech", fake_synthesize)

    handler._tts(" hello route ", voice_id="voice-route", speed=1.1)

    assert captured == {
        "text": "hello route",
        "voice_id": "voice-route",
        "speed": 1.1,
        "data": b"m4a-route-bytes",
        "content_type": "audio/mp4",
        "status": 200,
    }


def test_read_audio_upload_parses_multipart_audio() -> None:
    boundary = "----pai-test-boundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="audio"; filename="clip.webm"\r\n'
        "Content-Type: audio/webm\r\n"
        "\r\n"
        "audio-bytes\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="language"\r\n'
        "\r\n"
        "en\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    handler = Handler.__new__(Handler)
    handler.headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(body)),
    }
    handler.rfile = io.BytesIO(body)

    audio, filename, content_type, fields = handler._read_audio_upload()

    assert audio == b"audio-bytes"
    assert filename == "clip.webm"
    assert content_type == "audio/webm"
    assert fields == {"language": "en"}
