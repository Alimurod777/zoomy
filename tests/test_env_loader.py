import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

class EnvLoaderTests(unittest.TestCase):
    def test_paths_follow_project_after_move_and_ignore_working_directory(self):
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location('zoomy_start_paths', root / 'scripts/start-bot.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, clear=True):
            destination = Path(directory) / 'moved-project'
            path = Path(directory) / '.env'
            path.write_text('TELEGRAM_TOKEN=123:offline\nRECORDINGS_DIR=recordings\n'
                            'ZOOM_STATE_PATH=secrets/zoom_state.json\n'
                            'YOUTUBE_TOKEN_PATH=secrets/youtube_token.json\n')
            with patch.object(module, 'ROOT', destination):
                module.load_configuration(path)
            for name, relative in [('RECORDINGS_DIR', 'recordings'),
                                   ('ZOOM_STATE_PATH', 'secrets/zoom_state.json'),
                                   ('YOUTUBE_TOKEN_PATH', 'secrets/youtube_token.json')]:
                self.assertEqual(os.environ[name], str(destination / relative))

    def test_explicit_absolute_storage_path_is_preserved(self):
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location('zoomy_start_absolute', root / 'scripts/start-bot.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, clear=True):
            path = Path(directory) / '.env'
            path.write_text('TELEGRAM_TOKEN=123:offline\nRECORDINGS_DIR=/mnt/zoom-data\n')
            module.load_configuration(path)
            self.assertEqual(os.environ['RECORDINGS_DIR'], '/mnt/zoom-data')

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
