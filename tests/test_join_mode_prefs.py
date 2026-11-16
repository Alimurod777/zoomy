from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))
from join_modes import mode_for, set_mode


class CorruptPreferencesTests(unittest.TestCase):
    def test_corrupt_or_non_dict_file_falls_back_and_can_be_repaired(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict('os.environ', {'ZOOM_JOIN_MODE': ''}):
            path = Path(folder) / 'prefs.json'
            for content in ('', '{broken', '["web"]'):
                path.write_text(content)
                self.assertEqual(mode_for(1, path), 'auto')
            set_mode(1, 'web', path)
            self.assertEqual(mode_for(1, path), 'web')


if __name__ == '__main__':
    unittest.main()
