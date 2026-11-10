import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

class EnvLoaderTests(unittest.TestCase):
    def test_unquoted_mongo_uri_keeps_all_query_parameters(self):
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location('zoomy_start', root / 'scripts/start-bot.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        uri = 'mongodb+srv://example:password@cluster.example/?retryWrites=true&w=majority&appName=Zoomy'
        with tempfile.TemporaryDirectory(dir='/tmp') as directory, patch.dict(os.environ):
            path = Path(directory) / '.env'
            path.write_text('TELEGRAM_TOKEN=123:offline\nMONGODB_URI=' + uri + '\n')
            module.load_configuration(path)
            self.assertEqual(os.environ['MONGODB_URI'], uri)
