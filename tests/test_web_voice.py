from __future__ import annotations

import io
from pathlib import Path

import pytest

from usr.libexec.web.pai_web import actions
from usr.libexec.web.pai_web.server import Handler


def test_synthesize_speech_prefers_elevenlabs_when_key_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    class Response:
        content = b"mp3-bytes"

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setenv("ELEVENLABS_API_KEY", "eleven-test")
    monkeypatch.setenv("ELEVENLABS_MODEL_ID", "test-tts-model")
    monkeypatch.setattr(
        actions.requests,
        "post",
        lambda url, headers, params, json, timeout: captured.update(
            url=url,
            headers=headers,
            params=params,
            json=json,
            timeout=timeout,
        )
        or Response(),
    )

    audio = actions.synthesize_speech("hello", voice_id="voice-test", speed=2.0)

    assert audio == actions.SpeechAudio(b"mp3-bytes", "audio/mpeg")
    assert captured["url"] == "https://api.elevenlabs.io/v1/text-to-speech/voice-test"
    assert captured["headers"] == {"xi-api-key": "eleven-test", "accept": "audio/mpeg"}
    assert captured["params"] == {"output_format": "mp3_44100_128"}
    assert captured["json"] == {
        "text": "hello",
        "model_id": "test-tts-model",
        "voice_settings": {"speed": 1.2},
    }
    assert captured["timeout"] == 30


def test_synthesize_speech_falls_back_to_macos_say_without_elevenlabs_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs: list[dict] = []

    def fake_run(args, input, text, capture_output, check, timeout):
        runs.append(
            {
                "args": args,
                "input": input,
                "text": text,
                "capture_output": capture_output,
                "check": check,
                "timeout": timeout,
            }
        )
        if args[0] == "/usr/bin/say":
            Path(args[2]).write_bytes(b"aiff-bytes")
        elif args[0] == "/usr/bin/afconvert":
            Path(args[-1]).write_bytes(b"m4a-bytes")
        return actions.subprocess.CompletedProcess(args, 0)

    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.setattr(actions, "_reload_dotenv", lambda: None)
    monkeypatch.setattr(
        actions.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in {"say", "afconvert"} else None,
    )
    monkeypatch.setattr(actions.subprocess, "run", fake_run)
    monkeypatch.setattr(
        actions.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("ElevenLabs should not be called without a key"),
    )

    audio = actions.synthesize_speech("hello from fallback", voice_id="ignored", speed=0.8)

    assert audio == actions.SpeechAudio(b"m4a-bytes", "audio/mp4")
    assert len(runs) == 2
    assert runs[0]["args"][0] == "/usr/bin/say"
    assert "-v" not in runs[0]["args"]
    assert runs[0]["args"][3:] == ["-f", "-"]
    assert runs[0]["input"] == "hello from fallback"
    assert runs[0]["text"] is True
    assert runs[0]["capture_output"] is True
    assert runs[0]["check"] is True
    assert runs[0]["timeout"] == 60
    assert runs[1]["args"][0] == "/usr/bin/afconvert"
    assert runs[1]["args"][1:5] == ["-f", "m4af", "-d", "aac"]
    assert runs[1]["args"][-2].endswith("speech.aiff")
    assert runs[1]["args"][-1].endswith("speech.m4a")
    assert runs[1]["input"] is None
    assert runs[1]["timeout"] == 30


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


def test_transcribe_speech_requires_openai_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        actions.transcribe_speech(
            b"audio",
            filename="clip.webm",
            content_type="audio/webm",
        )


def test_transcribe_speech_posts_audio_to_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"text": "hello from voice"}

    def fake_post(**kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_TRANSCRIBE_MODEL", "test-transcribe")
    monkeypatch.delenv("OPENAI_TRANSCRIBE_LANGUAGE", raising=False)
    monkeypatch.delenv("OPENAI_TRANSCRIBE_PROMPT", raising=False)
    monkeypatch.setattr(
        actions.requests,
        "post",
        lambda url, headers, data, files, timeout: fake_post(
            url=url,
            headers=headers,
            data=data,
            files=files,
            timeout=timeout,
        ),
    )

    text = actions.transcribe_speech(
        b"audio-bytes",
        filename="clip.webm",
        content_type="audio/webm",
        language="en",
    )

    assert text == "hello from voice"
    assert captured["url"] == "https://api.openai.com/v1/audio/transcriptions"
    assert captured["headers"] == {"Authorization": "Bearer sk-test"}
    assert captured["data"] == {
        "model": "test-transcribe",
        "response_format": "json",
        "language": "en",
    }
    assert captured["files"] == {
        "file": ("clip.webm", b"audio-bytes", "audio/webm"),
    }
    assert captured["timeout"] == 60


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
