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

from PIL import Image


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


def clean_recording(path):
    """Two bounded-memory passes, atomic replace on success only."""
    info = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                    '-show_entries', 'stream=width,height,r_frame_rate', '-of', 'json', str(path)]))['streams'][0]
    width, height = info['width'], info['height']
    num, den = map(int, info['r_frame_rate'].split('/'))
    fps = num/den
    if not 1 <= fps <= 60 or width % 2 or height % 2:
        return 'unsupported'
    with tempfile.TemporaryDirectory(prefix='.native-view-', dir=path.parent) as folder:
        output = Path(folder) / 'clean.mp4'
        decoder = subprocess.Popen(['ffmpeg', '-nostdin', '-v', 'error', '-threads', '1', '-filter_threads', '1', '-i', str(path),
                    '-an', '-vf', 'scale=320:200:flags=neighbor', '-threads', '1',
                    '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'], stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL)
        frames = detected = 0
        first = None
        regions = []
        try:
            while True:
                raw = decoder.stdout.read(320*200*3)
                if not raw:
                    break
                if len(raw) != 320*200*3:
                    raise RuntimeError('Incomplete native analysis frame')
                thumbnail = Image.frombytes('RGB', (320,200), raw)
                # Scale the padded box back to the actual capture dimensions.
                box = media_bounds(thumbnail)
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
                        return 'uncertain'
                frames += 1
            if decoder.wait() != 0:
                raise RuntimeError('Native frame analysis failed')
        finally:
            decoder.stdout.close()
            if decoder.poll() is None:
                decoder.kill()
                decoder.wait()
        if not first or not frames or detected / frames < 0.6:
            return 'uncertain'
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
            '-c','copy','-movflags','+faststart',str(output)],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        if result.returncode or not output.is_file() or output.stat().st_size == 0:
            raise RuntimeError('Native surface encoding failed: ' + result.stderr.decode(errors='replace')[-400:])
        encoded=json.loads(subprocess.check_output(['ffprobe','-v','error','-select_streams','v:0',
            '-show_entries','stream=nb_frames,width,height','-of','json',str(output)]))['streams'][0]
        if (int(encoded.get('nb_frames',0))!=frames or encoded['width']!=out_w or encoded['height']!=out_h):
            raise RuntimeError('Native output frame count or geometry changed unexpectedly')
        os.chmod(output, path.stat().st_mode & 0o777)
        os.replace(output, path)
        return f'cleaned:{out_w}x{out_h}'


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
    args = parser.parse_args()
    if hasattr(os, 'nice'):
        os.nice(19)
    try:
        print(clean_recording(args.path), flush=True)
    except Exception as exc:
        print(type(exc).__name__, flush=True)
        raise SystemExit(1)
