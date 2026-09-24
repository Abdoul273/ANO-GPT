"""La réponse vision Azure ne doit pas attendre un autre déploiement lent."""

import threading
import time
from unittest.mock import patch

from core import azure_specialists


def test_first_vision_model_to_answer_returns_without_waiting_for_other():
    slow_started = threading.Event()
    release_slow = threading.Event()

    class Response:
        status_code = 200

        def __init__(self, answer):
            self.answer = answer

        def json(self):
            return {"choices": [{"message": {"content": self.answer}}]}

    def post(url, payload, timeout, **kwargs):
        if payload["model"] == "slow":
            slow_started.set()
            release_slow.wait(2)
            return Response("réponse tardive")
        assert slow_started.wait(1)
        return Response("Le bouton Wi-Fi est dans le panneau en haut à droite.")

    try:
        with patch.object(azure_specialists, "_load_config", return_value={
            "azure_openai_endpoint": "https://example.invalid",
            "azure_openai_api_key": "test",
        }), patch.object(azure_specialists, "vision_models", return_value=["slow", "fast"]), \
                patch.object(azure_specialists, "_azure_openai_endpoint", return_value="https://example.invalid/chat"), \
                patch.object(azure_specialists, "_post_with_retry", side_effect=post):
            started = time.monotonic()
            answer, model = azure_specialists.vision(b"image", "image/jpeg", "Où est le Wi-Fi ?")
            elapsed = time.monotonic() - started
        assert model == "fast"
        assert "Wi-Fi" in answer
        assert elapsed < 1
    finally:
        release_slow.set()
