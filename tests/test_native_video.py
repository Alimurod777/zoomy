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
from native_video import media_bounds, clean_recording, TerminalMatcher


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


def computer_frame():
    image=Image.new('RGB',(640,400),'black')
    draw=ImageDraw.Draw(image)
    draw.rectangle((0,0,639,24),fill=(40,40,40))
    draw.rectangle((0,25,639,375),fill='white')
    for x,y,color in ((0,25,'red'),(625,25,'blue'),(0,361,'yellow'),(625,361,'magenta')):
        draw.rectangle((x,y,x+14,y+14),fill=color)
    return image


def end_frame():
    image=Image.new('RGB',(640,400),(35,35,35))
    draw=ImageDraw.Draw(image)
    draw.rectangle((150,130,490,270),fill='white',outline='black',width=2)
    draw.rectangle((152,132,488,150),fill=(90,145,200))
    draw.text((170,175),'The host has ended this meeting',fill='black')
    draw.rectangle((380,235,465,255),fill=(0,112,243))
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

    def test_computer_share_touching_zoom_header_excludes_the_header(self):
        x,y,w,h=media_bounds(computer_frame())
        self.assertEqual(x,0)
        self.assertEqual(w,640)
        self.assertLessEqual(y,25)
        self.assertGreater(y,15)
        self.assertGreaterEqual(y+h,376)

    def test_plain_white_slide_does_not_match_confirmed_end_dialog(self):
        with tempfile.TemporaryDirectory() as directory:
            reference=Path(directory)/'end.png'
            end_frame().save(reference)
            matcher=TerminalMatcher(reference,(150,130,491,271))
            self.assertTrue(matcher.matches(end_frame().resize((320,200),Image.Resampling.BOX)))
            self.assertFalse(matcher.matches(Image.new('RGB',(320,200),'white')))
            self.assertFalse(matcher.matches(frame().resize((320,200),Image.Resampling.BOX)))

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
                source=frame(landscape=index>=8) if index<16 else computer_frame()
                proc.stdin.write(source.tobytes())
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

    def test_confirmed_end_dialog_tail_is_removed_at_first_matching_frame(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'end-test.mp4'
            reference=Path(directory)/'end.png'
            end_frame().save(reference)
            source=b''.join((frame() if index<18 else end_frame()).tobytes() for index in range(30))
            subprocess.run(['ffmpeg','-v','error','-y','-f','rawvideo','-pix_fmt','rgb24',
                '-s','640x400','-r','6','-i','-','-f','lavfi','-i','sine=frequency=440:sample_rate=48000',
                '-t','5','-c:v','libx264','-threads','1','-pix_fmt','yuv420p','-c:a','aac',str(path)],
                input=source,check=True)
            result=clean_recording(path,reference,(150,130,491,271),keep_full_view=True)
            self.assertIn('end-trimmed',result)
            info=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-of','json',str(path)]))
            video=next(s for s in info['streams'] if s['codec_type']=='video')
            audio=next(s for s in info['streams'] if s['codec_type']=='audio')
            self.assertEqual(int(video['nb_frames']),18)
            self.assertAlmostEqual(float(video['duration']),3,delta=.02)
            self.assertAlmostEqual(float(audio['duration']),3,delta=.04)


if __name__=='__main__':
    unittest.main()
