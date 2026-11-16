"""Verify finished captures contain a seekable, non-fragmented MP4 index."""
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))
from recorder import ZoomRecorder


def atoms(path):
    kinds=[]
    with path.open('rb') as stream:
        while header := stream.read(8):
            size, kind=struct.unpack('>I4s',header)
            kinds.append(kind)
            if size == 1:
                size=struct.unpack('>Q',stream.read(8))[0]
                stream.seek(size-16,1)
            elif size >= 8:
                stream.seek(size-8,1)
            else:
                break
    return kinds


class FinalizeTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg required')
    def test_single_capture_becomes_indexed_and_can_seek(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'recording.mp4'
            subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i',
                'testsrc2=size=64x64:rate=10','-t','4','-c:v','libx264','-threads','1',
                '-g','10','-movflags','+frag_keyframe+empty_moov+default_base_moof',str(path)],check=True)
            self.assertIn(b'moof',atoms(path))
            rec=ZoomRecorder()
            rec.output_path=path
            rec._segments=[path]
            rec._finalize_segments()
            final_atoms=atoms(path)
            self.assertNotIn(b'moof',final_atoms)
            self.assertLess(final_atoms.index(b'moov'),final_atoms.index(b'mdat'))
            result=subprocess.run(['ffmpeg','-nostdin','-v','error','-ss','3',
                '-i',str(path),'-frames:v','1','-f','image2pipe','-vcodec','png','-'],capture_output=True,check=True)
            self.assertTrue(result.stdout.startswith(b'\x89PNG'))

    def test_failed_remux_preserves_original_and_removes_partial_output(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'recording.mp4'
            path.write_bytes(b'original')
            rec=ZoomRecorder()
            rec.output_path=path
            rec._segments=[path]
            def fail(*args,**kwargs):
                path.with_suffix('.finalizing.mp4').write_bytes(b'partial')
                return SimpleNamespace(returncode=1)
            with patch('recorder.subprocess.run',side_effect=fail):
                rec._finalize_segments()
            self.assertEqual(path.read_bytes(),b'original')
            self.assertFalse(path.with_suffix('.finalizing.mp4').exists())
