"""Conservative native shared-screen framing, performed after capture.

Analyze every decoded frame before cropping: a phone rotation must never spend
the next polling interval inside the old portrait crop. The original recording
is retained unless both passes complete. Output aspect stays fixed; rotated
sources are fitted in full, with letterboxing when necessary.
"""
import argparse
from collections import deque
import json
import math
import os
import signal
import sys
from pathlib import Path
import subprocess
import tempfile

from PIL import Image, ImageStat


class TerminalMatcher:
    """Match a confirmed Zoom end window, never infer ending from stillness."""
    def __init__(self, reference, region):
        source = Image.open(reference).convert('RGB')
        width,height = source.size
        left,top,right,bottom = region
        if not (0<=left<right<=width and 0<=top<bottom<=height):
            raise ValueError('Invalid Zoom end-window region')
        self.box = (math.floor(left*320/width),math.floor(top*200/height),
                    math.ceil(right*320/width),math.ceil(bottom*200/height))
        self.reference = self._pixels(source.resize((320,200),Image.Resampling.BOX))
        sample = source.crop(region).convert('L')
        self.valid = ImageStat.Stat(sample).stddev[0] >= 12
        self.ink = [i for i,p in enumerate(self.reference) if min(p)<180 or max(p)-min(p)>40]
        self.valid = self.valid and len(self.ink)>=64

    def _pixels(self, frame):
        raw=frame.crop(self.box).resize((96,64),Image.Resampling.BOX).convert('RGB').tobytes()
        return list(zip(raw[::3],raw[1::3],raw[2::3]))

    def matches(self, frame):
        if not self.valid:
            return False
        pixels = self._pixels(frame)
        error = sum(sum(abs(a-b) for a,b in zip(p,q)) for p,q in zip(pixels,self.reference))/(len(pixels)*3)
        ink_matches = sum(max(abs(a-b) for a,b in zip(pixels[i],self.reference[i]))<=35 for i in self.ink)
        return error<=12 and ink_matches/len(self.ink)>=.9


def media_bounds(image):
    """Find one large rectangular central surface, rejecting thin toolbars.

    Uniform black margins separate the source from Zoom's headers, footers,
    and side windows. If that evidence is missing, return None rather than
    guessing fixed UI offsets. A little black padding protects source edges.
    """
    original_w, original_h = image.size
    small = image.convert('RGB').resize((320, 200), Image.Resampling.NEAREST)
    pixels = small.tobytes()
    mask = bytearray(max(pixels[i:i+3]) > 12 for i in range(0, len(pixels), 3))
    # Zoom's anchored gray header can touch a wide computer share without a
    # black gap. Remove it only when a short, uniform UI band is identifiable;
    # a gray source continuing beyond the band limit is left untouched.
    header_rows = 0
    for y in range(30):
        row = [pixels[(y*320+x)*3:(y*320+x)*3+3] for x in range(320)]
        gray = sum(28 <= min(p) <= max(p) <= 65 and max(p)-min(p)<15 for p in row)
        if gray/320 < .7:
            break
        header_rows += 1
    if 2 <= header_rows < 30:
        mask[:header_rows*320] = b'\0'*(header_rows*320)
    candidates = []
    for start in range(len(mask)):
        if not mask[start]:
            continue
        mask[start] = 0
        todo = deque([start])
        left = right = start % 320
        top = bottom = start // 320
        count = 0
        while todo:
            index = todo.popleft()
            x, y = index % 320, index // 320
            left, right = min(left, x), max(right, x)
            top, bottom = min(top, y), max(bottom, y)
            count += 1
            for neighbor in (index-320 if y else -1,
                             index+320 if y < 199 else -1,
                             index-1 if x else -1,
                             index+1 if x < 319 else -1):
                if neighbor >= 0 and mask[neighbor]:
                    mask[neighbor] = 0
                    todo.append(neighbor)
        width, height = right-left+1, bottom-top+1
        # Main surfaces surround the frame center and occupy most of at least
        # one axis. Floating audio dialogs, notifications and toolbars don't.
        if (left <= 160 <= right and top <= 100 <= bottom
                and width >= 30 and height >= 30
                and (width >= 192 or height >= 120)
                and count / (width * height) >= 0.65
                and width * height < 320 * 200 * 0.95):
            candidates.append((width*height, left, top, right+1, bottom+1))
    if not candidates:
        return None
    _, left, top, right, bottom = max(candidates)
    # Expand by two thumbnail pixels, round outward for the original frame.
    x = max(0, math.floor((left-2)*original_w/320/2)*2)
    y = max(0, math.floor((top-2)*original_h/200/2)*2)
    end_x = min(original_w, math.ceil((right+2)*original_w/320/2)*2)
    end_y = min(original_h, math.ceil((bottom+2)*original_h/200/2)*2)
    return (x, y, end_x-x, end_y-y)


def clean_recording(path, terminal_reference=None, terminal_region=None, keep_full_view=False):
    """Two bounded-memory passes, atomic replace on success only."""
    probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                    '-show_entries', 'stream=width,height,r_frame_rate:format=duration', '-of', 'json', str(path)]))
    info = probe['streams'][0]
    width, height = info['width'], info['height']
    num, den = map(int, info['r_frame_rate'].split('/'))
    fps = num/den
    if not 1 <= fps <= 60 or width % 2 or height % 2:
        return 'unsupported'
    matcher = TerminalMatcher(terminal_reference,terminal_region or (0,0,width,height)) if terminal_reference else None
    tail_start = max(0,int((float(probe.get('format',{}).get('duration',0))-30)*fps))
    with tempfile.TemporaryDirectory(prefix='.native-view-', dir=path.parent) as folder:
        output = Path(folder) / 'clean.mp4'
        decoder = subprocess.Popen(['ffmpeg', '-nostdin', '-v', 'error', '-threads', '1', '-filter_threads', '1', '-i', str(path),
                    '-an', '-vf', 'scale=320:200:flags=area', '-threads', '1',
                    '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'], stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL)
        frames = detected = 0
        first = None
        regions = []
        terminal_start = None
        try:
            while True:
                raw = decoder.stdout.read(320*200*3)
                if not raw:
                    break
                if len(raw) != 320*200*3:
                    raise RuntimeError('Incomplete native analysis frame')
                thumbnail = Image.frombytes('RGB', (320,200), raw)
                if matcher and frames>=tail_start and matcher.matches(thumbnail):
                    terminal_start = terminal_start or (frames,detected)
                else:
                    terminal_start = None
                # Scale the padded box back to the actual capture dimensions.
                box = media_bounds(thumbnail) if not keep_full_view else None
                if box:
                    x,y,w,h = box
                    left = max(0, math.floor(x*width/320/2)*2)
                    top = max(0, math.floor(y*height/200/2)*2)
                    right = min(width, math.ceil((x+w)*width/320/2)*2)
                    bottom = min(height, math.ceil((y+h)*height/200/2)*2)
                    box = (left,top,right-left,bottom-top)
                    first = first or box
                    detected += 1
                else:
                    # Never retain an old portrait crop after a rotation or
                    # an ambiguous/dark frame: preserve the complete input.
                    box = (0,0,width,height)
                if regions and all(abs(a-b) <= 12 for a,b in zip(box,regions[-1][1])):
                    # Merge minor border jitter by expanding outward. This
                    # avoids re-encoding a separate part for every pixel.
                    old=regions[-1][1]
                    left,top=min(old[0],box[0]),min(old[1],box[1])
                    right=max(old[0]+old[2],box[0]+box[2])
                    bottom=max(old[1]+old[3],box[1]+box[3])
                    regions[-1]=(regions[-1][0],(left,top,right-left,bottom-top))
                else:
                    regions.append((frames,box))
                    if len(regions)>256:
                            if not matcher:
                                return 'uncertain'
                            keep_full_view = True
                            first = None
                            regions = [(0,(0,0,width,height))]
                frames += 1
            if decoder.wait() != 0:
                raise RuntimeError('Native frame analysis failed')
        finally:
            decoder.stdout.close()
            if decoder.poll() is None:
                decoder.kill()
                decoder.wait()
        trimmed = False
        if terminal_start and 0<terminal_start[0]<frames-1:
            frames,detected = terminal_start
            regions = [(start,box) for start,box in regions if start<frames]
            trimmed = True
        if not first or not frames or detected / frames < 0.6 or keep_full_view:
            if not trimmed:
                return 'uncertain'
            first = (0,0,width,height)
            regions = [(0,first)]
        out_w, out_h = first[2:]
        # Encode constant-geometry intervals. Runtime crop-size changes stall
        # downstream scale/pad on some project FFmpeg builds. Every interval
        # uses the same canvas and frame rate, so rotations concatenate safely.
        parts=[]
        for index,(start,box) in enumerate(regions):
            end=regions[index+1][0] if index+1<len(regions) else frames
            x,y,w,h=box
            part=Path(folder)/f'part-{index:04d}.mp4'
            vf=(f'crop={w}:{h}:{x}:{y}:exact=1,'
                f'scale={out_w}:{out_h}:force_original_aspect_ratio=decrease:force_divisible_by=2,'
                f'pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1')
            result=subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-threads','1','-filter_threads','1',
                '-ss',f'{start/fps:.9f}','-i',str(path.resolve()),'-map','0:v:0','-an',
                '-frames:v',str(end-start),'-vf',vf,'-c:v','libx264','-preset','veryfast','-crf','23',
                '-threads','2','-pix_fmt','yuv420p',str(part)],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            if result.returncode:
                raise RuntimeError('Native interval encoding failed')
            parts.append(part.name)
        listing=Path(folder)/'parts.txt'
        listing.write_text(''.join(f"file '{name}'\n" for name in parts))
        result=subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-f','concat','-safe','0',
            '-i',str(listing),'-i',str(path.resolve()),'-map','0:v:0','-map','1:a?',
            '-t',f'{frames/fps:.9f}','-c','copy','-movflags','+faststart',str(output)],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        if result.returncode or not output.is_file() or output.stat().st_size == 0:
            raise RuntimeError('Native surface encoding failed: ' + result.stderr.decode(errors='replace')[-400:])
        encoded=json.loads(subprocess.check_output(['ffprobe','-v','error','-select_streams','v:0',
            '-show_entries','stream=nb_frames,width,height','-of','json',str(output)]))['streams'][0]
        if (int(encoded.get('nb_frames',0))!=frames or encoded['width']!=out_w or encoded['height']!=out_h):
            raise RuntimeError('Native output frame count or geometry changed unexpectedly')
        os.chmod(output, path.stat().st_mode & 0o777)
        os.replace(output, path)
        return (f'cleaned:{out_w}x{out_h}' + (':end-trimmed' if trimmed else '')
                + f':duration={frames/fps:.6f}')


if __name__ == '__main__':
    if sys.platform.startswith('linux') and os.getenv('ZOOMY_NATIVE_PARENT_PID'):
        import ctypes
        def parent_gone(signum, frame):
            # This worker owns its process group; stop its FFmpeg children too.
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            os.killpg(os.getpgrp(), signal.SIGTERM)
        signal.signal(signal.SIGTERM, parent_gone)
        if ctypes.CDLL(None).prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
            raise SystemExit(1)
        if os.getppid() != int(os.environ['ZOOMY_NATIVE_PARENT_PID']):
            parent_gone(signal.SIGTERM, None)
    parser = argparse.ArgumentParser()
    parser.add_argument('path', type=Path)
    parser.add_argument('--terminal-reference',type=Path)
    parser.add_argument('--terminal-region',type=lambda value:tuple(map(int,value.split(','))))
    parser.add_argument('--keep-full-view',action='store_true')
    args = parser.parse_args()
    if hasattr(os, 'nice'):
        os.nice(19)
    try:
        print(clean_recording(args.path,args.terminal_reference,args.terminal_region,args.keep_full_view), flush=True)
    except Exception as exc:
        print(type(exc).__name__, flush=True)
        raise SystemExit(1)
