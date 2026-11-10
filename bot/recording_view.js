// Draw only meeting media into the recording surface. Never crop the source.
(() => {
    if (window.__zoomyRecordingView) return;
    const style = document.createElement('style');
    style.textContent = `
      html[data-zoomy-recording] *, html[data-zoomy-recording] { cursor: none !important; }
      html[data-zoomy-recording] .single-suspension-container__self-video,
      html[data-zoomy-recording] .mobile-single-suspension-container__self-video,
      html[data-zoomy-recording] .footer__inner { visibility: hidden !important; }
      #zoomy-recording-surface { position:fixed!important; inset:0!important;
        width:100vw!important; height:100vh!important; margin:0!important;
        padding:0!important; border:0!important; z-index:2147483647!important;
        pointer-events:none!important; background:#000!important; }
    `;
    document.head.appendChild(style);
    document.documentElement.setAttribute('data-zoomy-recording', '');
    const surface = document.createElement('canvas');
    surface.id = 'zoomy-recording-surface';
    surface.setAttribute('aria-hidden', 'true');
    document.body.appendChild(surface);
    const context = surface.getContext('2d', {alpha: false, desynchronized: true});
    const sharedSelectors = [
        '.sharee-container__canvas', '.sharee-sp-container__canvas',
        '.sharee-container__viewport canvas', '.sharee-container__viewport video',
        '.sharee-sp-container__viewport canvas', '.sharee-sp-container__viewport video',
        '[data-testid="shared-screen"] canvas', '[data-testid="shared-screen"] video'
    ];
    const cameraSelectors = [
        '.speaker-active-container__video-frame canvas', '.speaker-active-container__video-frame video',
        '.speaker-active-container__wrap canvas', '.speaker-active-container__wrap video',
        '.speaker-active-container__video-stream',
        '[data-testid="active-speaker"] canvas', '[data-testid="active-speaker"] video'
    ];
    let source = null, relay = null, stream = null, lastProbe = -Infinity;
    let lastDraw = -Infinity, selectedMode = 'native', rectangle = null, frames = 0, stopped = false;
    let outputRegion = null;
    const dimensions = element => element instanceof HTMLVideoElement
        ? [element.videoWidth, element.videoHeight] : [element.width, element.height];
    const visible = element => {
        const rect = element.getBoundingClientRect();
        const computed = getComputedStyle(element);
        return rect.width > 1 && rect.height > 1 && computed.visibility !== 'hidden'
            && computed.display !== 'none' && computed.opacity !== '0';
    };
    function largest(selectors) {
        let best = null, area = 0;
        for (const element of document.querySelectorAll(selectors.join(','))) {
            if (!(element instanceof HTMLVideoElement || element instanceof HTMLCanvasElement)
                    || element === surface || !visible(element)) continue;
            const [w, h] = dimensions(element);
            if (w > 1 && h > 1 && w * h > area) { best = element; area = w * h; }
        }
        return best;
    }
    function releaseRelay() {
        if (stream) stream.getTracks().forEach(track => track.stop());
        if (relay) { relay.pause(); relay.srcObject = null; }
        stream = relay = null;
    }
    function select(next, mode) {
        if (source === next) { selectedMode = mode; return; }
        releaseRelay();
        source = next;
        selectedMode = mode;
        if (source instanceof HTMLCanvasElement && source.captureStream) {
            try {
                // Capture rendered frames, including WebGL canvases whose drawing
                // buffer is cleared after presentation. No screen permission needed.
                stream = source.captureStream(24);
                relay = document.createElement('video');
                relay.muted = relay.playsInline = true;
                relay.srcObject = stream;
                relay.play().catch(() => {});
                const track = stream.getVideoTracks()[0];
                if (track && track.requestFrame) track.requestFrame();
            } catch (_) { releaseRelay(); }
        }
    }
    function render(now) {
        if (stopped) return;
        if (now - lastProbe >= 250 || !source || !source.isConnected) {
            lastProbe = now;
            const shared = largest(sharedSelectors);
            select(shared || largest(cameraSelectors), shared ? 'shared-screen' : 'active-speaker');
        }
        if (source && now - lastDraw >= 1000 / 24) {
            const width = Math.max(1, window.innerWidth), height = Math.max(1, window.innerHeight);
            if (surface.width !== width || surface.height !== height) {
                surface.width = width; surface.height = height;
            }
            const frame = relay && relay.readyState >= 2 ? relay : source;
            const [w, h] = dimensions(frame);
            if (w > 1 && h > 1) {
                const area = outputRegion || {x:0, y:0, width, height};
                const scale = Math.min(area.width / w, area.height / h); // contain, never cover
                rectangle = {x: area.x + (area.width - w * scale) / 2, y: area.y + (area.height - h * scale) / 2,
                    width: w * scale, height: h * scale, sourceWidth: w, sourceHeight: h};
                try {
                    context.fillStyle = '#000'; context.fillRect(0, 0, width, height);
                    context.drawImage(frame, 0, 0, w, h, rectangle.x, rectangle.y, rectangle.width, rectangle.height);
                    surface.style.visibility = 'visible';
                    lastDraw = now; frames++;
                } catch (_) {
                    // Preserve the complete native view if media cannot be mirrored.
                    surface.style.visibility = outputRegion ? 'visible' : 'hidden';
                    selectedMode = outputRegion ? 'waiting' : 'native';
                }
            }
        } else if (!source) {
            // Once the capture area is fixed, never expose unrelated Zoom UI
            // underneath it while a presenter switches media.
            if (outputRegion) {
                context.fillStyle = '#000'; context.fillRect(0, 0, surface.width, surface.height);
                surface.style.visibility = 'visible'; selectedMode = 'waiting';
            } else { surface.style.visibility = 'hidden'; selectedMode = 'native'; }
            rectangle = null;
        }
        requestAnimationFrame(render);
    }
    window.__zoomyRecordingView = {
        status: () => ({mode: selectedMode, rectangle, frames, outputRegion}),
        setOutputRegion: region => {
            if (![region.x,region.y,region.width,region.height].every(Number.isInteger)
                    || region.x<0 || region.y<0 || region.width<2 || region.height<2
                    || region.x+region.width>innerWidth || region.y+region.height>innerHeight)
                throw new Error('Invalid recording area');
            outputRegion = {...region};lastDraw = -Infinity;
        },
        stop: () => { stopped = true; releaseRelay(); surface.remove(); style.remove();
            document.documentElement.removeAttribute('data-zoomy-recording'); delete window.__zoomyRecordingView; }
    };
    requestAnimationFrame(render);
})();
