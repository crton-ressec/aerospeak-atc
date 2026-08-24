import json
from urllib.error import HTTPError
from unittest.mock import patch

import server


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return json.dumps(self.body).encode()


def test_unavailable_configured_model_uses_supported_audio_fallback():
    requested_urls = []

    def fake_urlopen(request, timeout):
        requested_urls.append(request.full_url)
        if "retired-audio-model" in request.full_url:
            raise HTTPError(request.full_url, 404, "model not found", {}, None)
        return FakeResponse({
            "candidates": [{"content": {"parts": [{"text": "TRANSCRIPT: Radio check\nREPLY: Read you five by five."}]}}]
        })

    with patch.object(server, "urlopen", side_effect=fake_urlopen):
        reply = server.gemini_call([{"role": "user", "parts": [{"text": "test"}]}], model="retired-audio-model", retries=1)

    assert reply.endswith("Read you five by five.")
    assert any("models/retired-audio-model:generateContent" in url for url in requested_urls)
    assert any(f"models/{server.GEMINI_AUDIO_FALLBACK}:generateContent" in url for url in requested_urls)


if __name__ == "__main__":
    test_unavailable_configured_model_uses_supported_audio_fallback()
    print("AeroSpeak Gemini fallback tests passed")
