"""Import the real transcription module with optional environment values."""
import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch


class TranscriberConfigTests(unittest.TestCase):
    def test_optional_threads_are_usable_when_model_is_created(self):
        path = Path(__file__).resolve().parents[1] / "bot/transcriber.py"
        for value, expected in ((None, 2), ("", 2), ("  ", 2), ("2", 2), ("100",8)):
            with self.subTest(value=value), patch.dict(os.environ):
                if value is None:
                    os.environ.pop("WHISPER_THREADS", None)
                else:
                    os.environ["WHISPER_THREADS"] = value
                spec = importlib.util.spec_from_file_location("transcriber_config_test", path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                # No model download/inference: inspect the real wrapper call.
                with patch.object(module, "WhisperModel") as model:
                    module._get_model("tiny")
                self.assertEqual(model.call_args.kwargs["cpu_threads"], expected)
