import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))
from native_video import media_bounds, clean_recording


def frame(landscape=False):
    image = Image.new('RGB', (640,400), 'black')
    draw = ImageDraw.Draw(image)
    draw.rectangle((0,0,639,20), fill=(40,40,40))
    draw.rectangle((80,385,100,395), fill='white')
    draw.rectangle((560,385,580,395), fill='white')
    box = (30,90,610,310) if landscape else (240,30,400,370)
    draw.rectangle(box, fill=(40,180,80))
    left,top,right,bottom=box
    for x,y,color in ((left,top,'red'), (right-14,top,'blue'),
                      (left,bottom-14,'yellow'),(right-14,bottom-14,'magenta')):
        draw.rectangle((x,y,x+14,y+14),fill=color)
    return image


class GeometryTests(unittest.TestCase):
    def test_portrait_excludes_header_toolbar_and_side_panel(self):
        image=frame()
        ImageDraw.Draw(image).rectangle((540,40,639,375),fill=(60,60,60))
        box=media_bounds(image)
        self.assertIsNotNone(box)
        x,y,w,h=box
        self.assertLessEqual(x,240)
        self.assertGreaterEqual(x+w,401)
        self.assertLessEqual(y,30)
        self.assertGreaterEqual(y+h,371)
        self.assertGreater(x,200)
        self.assertLess(x+w,500)
        self.assertGreater(y,20)
        self.assertLess(y+h,385)

    def test_rotation_expands_to_include_both_landscape_edges(self):
        x,y,w,h=media_bounds(frame(True))
        self.assertLessEqual(x,30)
        self.assertGreaterEqual(x+w,611)
        self.assertLessEqual(y,90)
        self.assertGreaterEqual(y+h,311)

    def test_audio_dialog_or_toolbar_is_not_a_video_surface(self):
        image=Image.new('RGB',(640,400),'black')
        draw=ImageDraw.Draw(image)
        draw.rectangle((0,0,639,20),fill=(40,40,40))
        draw.rectangle((220,140,420,260),fill='white')
        self.assertIsNone(media_bounds(image))

    def test_blank_or_unseparated_surfaces_are_not_guessed(self):
        self.assertIsNone(media_bounds(Image.new('RGB',(640,400),'black')))
        self.assertIsNone(media_bounds(Image.new('RGB',(640,400),'white')))


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'requires project FFmpeg runtime')
class EncodeTests(unittest.TestCase):
    def test_portrait_rotation_keeps_four_corners_audio_and_duration(self):
        with tempfile.TemporaryDirectory(prefix='native-video-') as directory:
            path=Path(directory)/'phone.mp4'
            proc=subprocess.Popen(['ffmpeg','-v','error','-y','-f','rawvideo','-pix_fmt','rgb24',
                 '-s','640x400','-r','6','-i','-','-f','lavfi','-i','sine=frequency=440:sample_rate=48000',
                 '-t','4','-c:v','libx264','-threads','1','-pix_fmt','yuv420p','-crf','18',
                 '-c:a','aac',str(path)],stdin=subprocess.PIPE,stderr=subprocess.PIPE)
            for index in range(24):
                proc.stdin.write(frame(landscape=index>=12).tobytes())
            proc.stdin.close()
            error=proc.stderr.read()
            self.assertEqual(proc.wait(),0,error.decode())
            proc.stderr.close()
            before=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-of','json',str(path)]))
            outcome=clean_recording(path)
            self.assertTrue(outcome.startswith('cleaned:'),outcome)
            after=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-of','json',str(path)]))
            video=next(s for s in after['streams'] if s['codec_type']=='video')
            self.assertLess(video['width'],220)
            self.assertGreater(video['height'],330)
            self.assertAlmostEqual(float(video['duration']),4,delta=.2)
            self.assertEqual(next(s for s in before['streams'] if s['codec_type']=='audio')['duration'],
                             next(s for s in after['streams'] if s['codec_type']=='audio')['duration'])
            for timestamp in ('1','2','3'):
                raw=subprocess.check_output(['ffmpeg','-v','error','-ss',timestamp,'-i',str(path),
                    '-frames:v','1','-threads','1','-f','rawvideo','-pix_fmt','rgb24','-'])
                pixels=list(zip(raw[::3],raw[1::3],raw[2::3]))
                for predicate in (lambda r,g,b:r>150 and g<100 and b<100,
                                  lambda r,g,b:b>130 and r<100 and g<100,
                                  lambda r,g,b:r>140 and g>140 and b<100,
                                  lambda r,g,b:r>140 and b>100 and g<100):
                    self.assertTrue(any(predicate(*p) for p in pixels),f'corner missing at {timestamp}')

    def test_uncertain_video_is_kept_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'blank.mp4'
            subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=black:s=640x400:r=6',
                '-t','1','-c:v','libx264','-threads','1',str(path)],check=True)
            original=path.read_bytes()
            self.assertEqual(clean_recording(path),'uncertain')
            self.assertEqual(path.read_bytes(),original)

    def test_encoder_failure_keeps_original_and_removes_temporary_parts(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'phone.mp4'
            subprocess.run(['ffmpeg','-v','error','-y','-f','rawvideo','-pix_fmt','rgb24',
                '-s','640x400','-r','1','-i','-','-c:v','libx264','-threads','1',str(path)],
                input=frame().tobytes(),check=True)
            original=path.read_bytes()
            real_run=subprocess.run
            def fail_encoder(args,**kwargs):
                if '-vf' in args and any('crop=' in arg for arg in args):
                    return subprocess.CompletedProcess(args,1,b'',b'offline encoding failure')
                return real_run(args,**kwargs)
            with patch('native_video.subprocess.run',side_effect=fail_encoder):
                with self.assertRaises(RuntimeError):
                    clean_recording(path)
            self.assertEqual(path.read_bytes(),original)
            self.assertFalse(list(Path(directory).glob('.native-view-*')))


if __name__=='__main__':
    unittest.main()
