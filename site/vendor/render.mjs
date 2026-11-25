// Clip Studio: make an edited clip ON THE PHONE in a few seconds (like CapCut / TikTok's editor) - Nov 25.
// The phone's own video chip decodes the no-caption copy, draws the hook + follow pop-up (+ progress bar) on every
// frame, and encodes it again (WebCodecs). The sound is copied as is. Result: a finished MP4, no server needed.
// Libraries: mp4box.js (reads MP4, BSD-3) and mp4-muxer (writes MP4, MIT), both in this folder.
import { Muxer, ArrayBufferTarget } from './mp4-muxer.min.mjs';

const W = 1080, H = 1920, FPS = 30;

function loadScript(src) {
  return new Promise((res, rej) => {
    if (window.MP4Box) return res();
    const s = document.createElement('script'); s.src = src; s.onload = res; s.onerror = () => rej(new Error('could not load ' + src));
    document.head.append(s);
  });
}

export function supported() {
  return typeof window.VideoEncoder === 'function' && typeof window.VideoDecoder === 'function' && typeof window.VideoFrame === 'function';
}

function findTag(d, tag) {   // the AAC "AudioSpecificConfig" inside the esds box
  if (!d) return null;
  if (d.tag === tag) return d;
  for (const c of d.descs || []) { const f = findTag(c, tag); if (f) return f; }
  return null;
}

// ---- the follow pop-up, drawn exactly like engage.py (pop in 0.7 s, pulse, pop out at ~3.95 s)
const START = 0.7, IN_S = 0.35, HOLD_S = 2.6, OUT_S = 0.3;
function easeBack(x) { const c1 = 1.70158, c3 = c1 + 1; return 1 + c3 * (x - 1) ** 3 + c1 * (x - 1) ** 2; }
function ctaState(t) {
  const u = t - START; if (u < 0 || u > IN_S + HOLD_S + OUT_S) return null;
  if (u < IN_S) { const x = u / IN_S; return [0.55 + 0.45 * easeBack(x), Math.min(1, x * 1.6)]; }
  if (u < IN_S + HOLD_S) { const v = (u - IN_S) / HOLD_S; return [1 + (v >= 0.35 && v <= 0.65 ? 0.06 * Math.sin((v - 0.35) / 0.3 * Math.PI) : 0), 1]; }
  const x = (u - IN_S - HOLD_S) / OUT_S; return [1 - 0.25 * x, Math.max(0, 1 - x)];
}
export const CTA_H = 104;
function drawCta(g, t, cy) {
  const st = ctaState(t); if (!st) return;
  const [s, a] = st; const TEXT = 'Follow for daily clips';
  g.save(); g.globalAlpha = a;
  g.font = '700 46px Inter, -apple-system, "Helvetica Neue", Arial, sans-serif';
  const tw = g.measureText(TEXT).width; const h = 104, circ = 72, pad = 16; const w = pad + circ + 22 + tw + 34;
  g.translate(W / 2, cy); g.scale(s, s); g.translate(-w / 2, -h / 2);
  g.fillStyle = '#fff'; g.beginPath(); if (g.roundRect) g.roundRect(0, 0, w, h, h / 2); else g.rect(0, 0, w, h); g.fill();
  const cx = pad + circ / 2, cyy = h / 2;
  g.fillStyle = '#ff2e88'; g.beginPath(); g.arc(cx, cyy, circ / 2, 0, Math.PI * 2); g.fill();
  g.fillStyle = '#fff'; g.fillRect(cx - 17, cyy - 3.5, 34, 7); g.fillRect(cx - 3.5, cyy - 17, 7, 34);
  g.fillStyle = '#0a0a0e'; g.textBaseline = 'middle'; g.fillText(TEXT, pad + circ + 22, cyy + 2);
  g.restore();
}

/**
 * src: URL of the no-caption copy.  a, b: the part to keep (seconds in that video).
 * hook: canvas/image of the hook box (1080-wide units) or null; hookY: its top (px).  ctaY: centre of the pop-up.
 * pbar: draw the progress bar.  onPct(0..1).  Returns a Blob (video/mp4).
 */
export async function phoneRender({ src, a = 0, b = Infinity, hook = null, hookY = 0, ctaY = 800, pbar = false, onPct = () => {} }) {
  if (!supported()) throw new Error('this phone can\'t make videos in the browser');
  await loadScript(new URL('./mp4box.all.js', import.meta.url).href);
  const buf = await (await fetch(src)).arrayBuffer();
  buf.fileStart = 0;
  const file = window.MP4Box.createFile();
  let info = null;
  file.onReady = (i) => { info = i; };
  file.onError = (e) => { throw new Error('bad video: ' + e); };
  file.appendBuffer(buf); file.flush();
  if (!info) throw new Error('could not read the video');
  const vt = info.videoTracks[0], at = info.audioTracks[0];
  if (!vt) throw new Error('no picture in the video');
  const vs = [], as = [];
  file.onSamples = (id, user, samples) => { (user === 'v' ? vs : as).push(...samples); };
  file.setExtractionOptions(vt.id, 'v', { nbSamples: 1e9 });
  if (at) file.setExtractionOptions(at.id, 'a', { nbSamples: 1e9 });
  file.start(); file.flush();

  const trakV = file.getTrackById(vt.id);
  const entry = trakV.mdia.minf.stbl.stsd.entries[0];
  const box = entry.avcC || entry.hvcC;
  const ds = new window.DataStream(undefined, 0, window.DataStream.BIG_ENDIAN); box.write(ds);
  const description = new Uint8Array(ds.buffer, 8);
  const shift = (trak) => { const e = trak.edts && trak.edts.elst && trak.edts.elst.entries && trak.edts.elst.entries[0];
    return e && e.media_time > 0 ? e.media_time : 0; };   // ffmpeg's start delay (B-frames / AAC priming)
  const vShift = shift(trakV);
  const dur = (vs.length ? (vs[vs.length - 1].cts + vs[vs.length - 1].duration - vShift) / vs[0].timescale : 0);
  b = Math.min(b, dur); a = Math.max(0, Math.min(a, b - 0.5));

  // ---- encoder: the best H.264 profile this phone has
  let cfg = null;
  for (const codec of ['avc1.640028', 'avc1.4d0028', 'avc1.42e028', 'avc1.640033']) {
    const c = { codec, width: W, height: H, bitrate: 9_000_000, framerate: FPS, avc: { format: 'avc' }, hardwareAcceleration: 'prefer-hardware' };
    try { if ((await window.VideoEncoder.isConfigSupported(c)).supported) { cfg = c; break; } } catch (e) { /* try the next */ }
    try { delete c.hardwareAcceleration; if ((await window.VideoEncoder.isConfigSupported(c)).supported) { cfg = c; break; } } catch (e) { /* */ }
  }
  if (!cfg) throw new Error('no H.264 encoder on this phone');
  const target = new ArrayBufferTarget();
  let aCfg = null;
  if (at) {
    const esds = file.getTrackById(at.id).mdia.minf.stbl.stsd.entries[0].esds;
    const dsi = esds && findTag(esds.esd, 5);
    aCfg = { codec: at.codec || 'mp4a.40.2', sampleRate: at.audio.sample_rate, numberOfChannels: at.audio.channel_count,
             description: dsi ? new Uint8Array(dsi.data) : undefined };
  }
  const muxer = new Muxer({ target, video: { codec: 'avc', width: W, height: H, frameRate: FPS },
    audio: aCfg ? { codec: 'aac', sampleRate: aCfg.sampleRate, numberOfChannels: aCfg.numberOfChannels } : undefined,
    fastStart: 'in-memory', firstTimestampBehavior: 'offset' });
  let failed = null;
  const enc = new window.VideoEncoder({ output: (chunk, meta) => muxer.addVideoChunk(chunk, meta), error: (e) => { failed = e; } });
  enc.configure(cfg);

  // ---- drawing surface
  const cv = typeof OffscreenCanvas === 'function' ? new OffscreenCanvas(W, H) : Object.assign(document.createElement('canvas'), { width: W, height: H });
  const g = cv.getContext('2d');
  let n = 0;
  const total = Math.max(0.1, b - a);
  const dec = new window.VideoDecoder({
    output: (frame) => {
      const t = frame.timestamp / 1e6;
      if (t < a - 0.001 || t >= b || failed) { frame.close(); return; }
      const rt = t - a;
      g.drawImage(frame, 0, 0, W, H);
      frame.close();
      if (hook) g.drawImage(hook, (W - hook.width) / 2, hookY);
      drawCta(g, rt, ctaY);
      if (pbar) { g.fillStyle = 'rgba(0,0,0,0.35)'; g.fillRect(0, 0, W, 10); g.fillStyle = '#ff2e88'; g.fillRect(0, 0, W * Math.min(1, rt / total), 10); }
      const vf = new window.VideoFrame(cv, { timestamp: Math.round(rt * 1e6), duration: Math.round(1e6 / FPS) });
      enc.encode(vf, { keyFrame: n % (FPS * 2) === 0 });
      vf.close(); n++;
      onPct(Math.min(0.99, rt / total));
    },
    error: (e) => { failed = e; },
  });
  let dcfg = { codec: vt.codec, codedWidth: vt.video.width, codedHeight: vt.video.height, description, hardwareAcceleration: 'prefer-hardware' };
  try { if (!(await window.VideoDecoder.isConfigSupported(dcfg)).supported) { delete dcfg.hardwareAcceleration; } } catch (e) { delete dcfg.hardwareAcceleration; }
  try { if (!(await window.VideoDecoder.isConfigSupported(dcfg)).supported) throw new Error('this phone can\'t read ' + vt.codec); } catch (e) { throw e; }
  dec.configure(dcfg);
  const ts = vs[0].timescale;
  let first = 0;   // decode from the last keyframe at or before the start
  for (let i = 0; i < vs.length; i++) { if (vs[i].is_sync && (vs[i].cts - vShift) / ts <= a + 0.001) first = i; }
  for (let i = first; i < vs.length; i++) {
    const s = vs[i];
    if ((s.dts - vShift) / ts > b + 0.6) break;
    while ((dec.decodeQueueSize > 6 || enc.encodeQueueSize > 6) && !failed) await new Promise((r) => setTimeout(r, 4));
    if (failed) break;
    dec.decode(new window.EncodedVideoChunk({ type: s.is_sync ? 'key' : 'delta', timestamp: Math.round((s.cts - vShift) / ts * 1e6),
      duration: Math.round(s.duration / ts * 1e6), data: s.data }));
  }
  if (failed) throw new Error('video decode/encode failed: ' + (failed.message || failed));
  await dec.flush(); await enc.flush();
  dec.close(); enc.close();
  if (failed) throw failed;
  if (!n) throw new Error('no frames came out');
  // ---- sound: copied as is (no quality loss)
  if (aCfg && as.length) {
    const aShift = shift(file.getTrackById(at.id)); const ats = as[0].timescale; let firstA = true;
    for (const s of as) {
      const t = (s.cts - aShift) / ats;
      if (t < a - 0.001 || t >= b) continue;
      muxer.addAudioChunkRaw(new Uint8Array(s.data), 'key', Math.round((t - a) * 1e6), Math.round(s.duration / ats * 1e6),
        firstA ? { decoderConfig: { codec: aCfg.codec, sampleRate: aCfg.sampleRate, numberOfChannels: aCfg.numberOfChannels, description: aCfg.description } } : undefined);
      firstA = false;
    }
  }
  muxer.finalize();
  onPct(1);
  return new Blob([target.buffer], { type: 'video/mp4' });
}
