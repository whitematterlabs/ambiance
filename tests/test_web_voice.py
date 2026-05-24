from __future__ import annotations

import io

import pytest

from usr.libexec.web.pai_web import actions
from usr.libexec.web.pai_web.server import Handler


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
