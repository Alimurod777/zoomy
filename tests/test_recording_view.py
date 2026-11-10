import asyncio
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))
from recorder import ZoomRecorder

SCRIPT = (ROOT / "bot/recording_view.js").read_text()
HTML = '''<style>body{margin:0;background:#ddd}.single-suspension-container__self-video{
position:fixed;top:0;left:0;width:150px;height:60px;background:purple}
.footer__inner{position:fixed;bottom:0;height:60px;width:100%;background:pink}
canvas{width:200px;height:300px}</style>
<div class="single-suspension-container__self-video">BOT PROFILE</div>
<div class="speaker-active-container__video-frame"><canvas id="speaker" width="640" height="360"></canvas></div>
<div class="sharee-container__viewport"><canvas id="shared" class="sharee-container__canvas" width="320" height="640"></canvas></div>
<div class="footer__inner">MEETING BUTTONS</div>
<script>
function paint(w,h){const c=document.querySelector('#shared');c.width=w;c.height=h;
 const x=c.getContext('2d');x.fillStyle='red';x.fillRect(0,0,w/2,h/2);
 x.fillStyle='lime';x.fillRect(w/2,0,w/2,h/2);x.fillStyle='blue';x.fillRect(0,h/2,w/2,h/2);
 x.fillStyle='yellow';x.fillRect(w/2,h/2,w/2,h/2);}
paint(320,640);
const speaker=document.querySelector('#speaker').getContext('2d');speaker.fillStyle='#00ffff';speaker.fillRect(0,0,640,360);
</script>'''


class RecordingViewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(headless=True,
            args=["--no-sandbox", "--autoplay-policy=no-user-gesture-required"])
        self.page = await self.browser.new_page(viewport={"width": 960, "height": 540})
        await self.page.set_content(HTML)
        await self.page.evaluate(SCRIPT)
        await self.page.wait_for_function("window.__zoomyRecordingView.status().frames > 4")

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()

    async def assert_all_corners(self, width, height):
        await self.page.wait_for_function("([w,h])=>{const r=window.__zoomyRecordingView.status().rectangle;return r?.sourceWidth===w&&r?.sourceHeight===h}", arg=[width,height])
        await self.page.wait_for_timeout(150)
        sample = await self.page.evaluate('''() => {
            const r=window.__zoomyRecordingView.status().rectangle;
            const c=document.querySelector('#zoomy-recording-surface').getContext('2d');
            const at=(x,y)=>Array.from(c.getImageData(Math.floor(x),Math.floor(y),1,1).data).slice(0,3);
            return {r,colors:[at(r.x+3,r.y+3),at(r.x+r.width-3,r.y+3),
                at(r.x+3,r.y+r.height-3),at(r.x+r.width-3,r.y+r.height-3)]};
        }''')
        self.assertEqual(sample["colors"], [[255,0,0],[0,255,0],[0,0,255],[255,255,0]])
        rect = sample["r"]
        self.assertGreaterEqual(rect["x"], 0)
        self.assertGreaterEqual(rect["y"], 0)
        self.assertLessEqual(rect["x"] + rect["width"], 960)
        self.assertLessEqual(rect["y"] + rect["height"], 540)
        self.assertAlmostEqual(rect["width"] / rect["height"], width / height)

    async def test_portrait_share_has_all_four_edges_and_no_profile_or_footer(self):
        await self.assert_all_corners(320, 640)
        self.assertEqual((await self.page.evaluate("window.__zoomyRecordingView.status()"))["mode"], "shared-screen")
        self.assertFalse(await self.page.locator('.single-suspension-container__self-video').is_visible())
        self.assertFalse(await self.page.locator('.footer__inner').is_visible())

    async def test_landscape_share_resizes_without_cutting_content(self):
        await self.page.evaluate("paint(1280,720)")
        await self.assert_all_corners(1280, 720)

    async def test_camera_returns_when_screen_share_stops(self):
        await self.page.locator('#shared').evaluate('(element)=>element.remove()')
        await self.page.wait_for_function("window.__zoomyRecordingView.status().mode==='active-speaker'")
        await self.page.wait_for_timeout(150)
        pixel = await self.page.evaluate("Array.from(document.querySelector('#zoomy-recording-surface').getContext('2d').getImageData(480,270,1,1).data).slice(0,3)")
        self.assertEqual(pixel, [0,255,255])

    async def test_portrait_output_removes_only_unused_side_margins(self):
        recorder = ZoomRecorder()
        await recorder._configure_capture_bounds(self.page)
        bounds = recorder._capture_bounds
        self.assertEqual((bounds['width'], bounds['height']), (270, 540))
        self.assertEqual(bounds['x'], 345)
        self.assertIn('crop=270:540:345:87:exact=1', recorder._build_vf(87))
        await self.assert_all_corners(320, 640)

    async def test_new_source_aspect_fits_existing_output_without_cutting_edges(self):
        recorder = ZoomRecorder()
        await recorder._configure_capture_bounds(self.page)
        bounds = dict(recorder._capture_bounds)
        await self.page.evaluate('paint(1280,720)')
        await self.assert_all_corners(1280, 720)
        state = await self.page.evaluate('window.__zoomyRecordingView.status()')
        rect = state['rectangle']
        self.assertGreaterEqual(rect['x'], bounds['x'])
        self.assertGreaterEqual(rect['y'], bounds['y'])
        self.assertLessEqual(rect['x'] + rect['width'], bounds['x'] + bounds['width'])
        self.assertLessEqual(rect['y'] + rect['height'], bounds['y'] + bounds['height'])
        self.assertEqual(recorder._capture_bounds, bounds)

    async def test_fixed_output_does_not_reveal_panels_when_media_disappears(self):
        recorder = ZoomRecorder()
        await recorder._configure_capture_bounds(self.page)
        await self.page.locator('#shared, #speaker').evaluate_all('(elements)=>elements.forEach(e=>e.remove())')
        await self.page.wait_for_function("window.__zoomyRecordingView.status().mode==='waiting'")
        self.assertTrue(await self.page.locator('#zoomy-recording-surface').is_visible())

    async def test_overlay_does_not_block_zoom_controls_and_can_be_restored(self):
        events = await self.page.locator('#zoomy-recording-surface').evaluate('(element)=>getComputedStyle(element).pointerEvents')
        self.assertEqual(events, "none")
        await self.page.evaluate("window.__zoomyRecordingView.stop()")
        self.assertTrue(await self.page.locator('.footer__inner').is_visible())
        self.assertEqual(await self.page.locator('#zoomy-recording-surface').count(), 0)


    async def test_live_webgl_shared_frames_are_not_frozen_or_black(self):
        available = await self.page.evaluate('''() => {
            const old=document.querySelector('#shared'), c=document.createElement('canvas');
            c.id='shared';c.className=old.className;c.width=320;c.height=640;old.replaceWith(c);
            const gl=c.getContext('webgl',{preserveDrawingBuffer:false});
            if(!gl)return false;
            window.testGLColor=[1,0,0,1];
            function draw(){gl.clearColor(...window.testGLColor);gl.clear(gl.COLOR_BUFFER_BIT);requestAnimationFrame(draw);}
            requestAnimationFrame(draw);return true;
        }''')
        if not available:
            self.skipTest('WebGL is unavailable in this browser')
        await self.page.wait_for_function('''() => {
            const p=document.querySelector('#zoomy-recording-surface').getContext('2d').getImageData(480,270,1,1).data;
            return p[0]>230&&p[1]<20&&p[2]<20;
        }''')
        await self.page.evaluate('window.testGLColor=[0,1,0,1]')
        await self.page.wait_for_function('''() => {
            const p=document.querySelector('#zoomy-recording-surface').getContext('2d').getImageData(480,270,1,1).data;
            return p[1]>230&&p[0]<20&&p[2]<20;
        }''')


class RecordingEncoderTests(unittest.TestCase):
    def test_encoder_hides_pointer_and_preserves_odd_content_height(self):
        recorder = ZoomRecorder()
        with tempfile.TemporaryDirectory(dir="/tmp") as directory, patch("recorder.subprocess.Popen") as process:
            recorder._start_ffmpeg(str(Path(directory) / "video.mp4"), 87)
            recorder._ffmpeg_log.close()
        command = process.call_args.args[0]
        self.assertEqual(command[command.index('-draw_mouse') + 1], '0')
        filters = command[command.index('-vf') + 1]
        self.assertIn('crop=1920:1113:0:87:exact=1', filters)
        self.assertIn('pad=1920:ceil(ih/2)*2', filters)
