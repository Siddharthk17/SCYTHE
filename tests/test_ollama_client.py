import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from ctx_engine.daemon.local_llm import (
    is_ollama_available,
    get_available_models,
    select_model,
    OllamaClient,
    PREFERRED_MODELS,
)


class TestOllamaClientIsAvailable:
    @patch("urllib.request.urlopen")
    def test_is_ollama_available_true(self, mock_urlopen):
        resp = MagicMock()
        resp.status = 200
        mock_urlopen.return_value.__enter__.return_value = resp
        assert is_ollama_available() is True

    @patch("urllib.request.urlopen", side_effect=Exception("Connection refused"))
    def test_is_ollama_available_false(self, mock_urlopen):
        assert is_ollama_available() is False


class TestGetAvailableModels:
    @patch("urllib.request.urlopen")
    def test_returns_model_names(self, mock_urlopen):
        resp = MagicMock()
        resp.read.return_value = json.dumps(
            {"models": [{"name": "llama3.2:3b"}]}
        ).encode()
        mock_urlopen.return_value.__enter__.return_value = resp
        models = get_available_models()
        assert models == ["llama3.2:3b"]

    @patch("urllib.request.urlopen", side_effect=Exception("fail"))
    def test_returns_empty_on_error(self, mock_urlopen):
        assert get_available_models() == []


class TestSelectModel:
    def test_returns_exact_override(self):
        available = ["codellama:7b-instruct", "other:latest"]
        assert select_model(available, override="codellama:7b-instruct") == "codellama:7b-instruct"

    def test_returns_none_for_unavailable_override(self):
        available = ["llama3.2:3b-instruct"]
        assert select_model(available, override="nonexistent:latest") is None

    def test_returns_first_preferred(self):
        available = ["other:latest", "qwen2.5-coder:7b-instruct", "llama3.2:3b-instruct"]
        assert select_model(available) == "qwen2.5-coder:7b-instruct"

    def test_returns_none_when_not_available(self):
        assert select_model([]) is None


class TestOllamaClient:
    def test_constructor_starts_thread(self):
        def conn_factory():
            return MagicMock()
        client = OllamaClient(
            model="qwen2.5-coder:7b",
            host="http://localhost:11434",
            conn_factory=conn_factory,
            repo_root=Path("/tmp"),
        )
        assert client._model == "qwen2.5-coder:7b"
        assert client._thread is not None
        assert client._thread.daemon is True
        client.stop()
