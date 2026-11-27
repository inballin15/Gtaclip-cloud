// Clip Studio: make an edited clip ON THE PHONE in a few seconds (like CapCut / TikTok's editor) - Nov 25.
// The phone's own video chip decodes the no-caption copy, draws the hook + follow pop-up (+ progress bar) on every
// frame, and encodes it again (WebCodecs). The sound is copied as is. Result: a finished MP4, no server needed.
// Libraries: mp4box.js (reads MP4, BSD-3) and mp4-muxer (writes MP4, MIT), both in this folder.
import { Muxer, ArrayBufferTarget } from './mp4-muxer.min.mjs';

const W = 1080, H = 1920;

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
export function drawCta(g, t, cy) {
  const st = ctaState(t); if (!st) return;
  const [s, a] = st; const TEXT = 'Follow & Subscribe', TEXT2 = 'for daily clips';
  g.save(); g.globalAlpha = a;
  const F1 = '700 46px Inter, -apple-system, "Helvetica Neue", Arial, sans-serif', F2 = '600 34px Inter, -apple-system, "Helvetica Neue", Arial, sans-serif';
  g.font = F1; const tw = g.measureText(TEXT).width; g.font = F2; const tw2 = g.measureText(TEXT2).width;
  const h = 104, circ = 72, pad = 16; const w = pad + circ + 22 + tw + 14 + tw2 + 36;
  g.translate(W / 2, cy); g.scale(s, s); g.translate(-w / 2, -h / 2);
  g.fillStyle = '#fff'; g.beginPath(); if (g.roundRect) g.roundRect(0, 0, w, h, h / 2); else g.rect(0, 0, w, h); g.fill();
  const cx = pad + circ / 2, cyy = h / 2;
  g.fillStyle = '#ff2e88'; g.beginPath(); g.arc(cx, cyy, circ / 2, 0, Math.PI * 2); g.fill();
  g.fillStyle = '#fff'; g.fillRect(cx - 17, cyy - 3.5, 34, 7); g.fillRect(cx - 3.5, cyy - 17, 7, 34);
  g.textBaseline = 'middle';
  g.font = F1; g.fillStyle = '#0a0a0e'; g.fillText(TEXT, pad + circ + 22, cyy + 2);
  g.font = F2; g.fillStyle = '#6e6a80'; g.fillText(TEXT2, pad + circ + 22 + tw + 14, cyy + 4);
  g.restore();
}

// ---- the sound like engage.py: the clip, a "pop" as the button appears, and the spoken follow line right after it
// (the clip dips ~3 dB for the pop and ~11 dB under the voice, then straight back). Made by the phone's audio
// engine (OfflineAudioContext) and its AAC encoder (WebCodecs). null = this phone can't: the sound is copied as is.
export const POP_AT = START + 0.06, SAY_AT = POP_AT + 0.22, SAY_DUCK = 0.12, SAY_GAIN = 1.8;
export function popBuffer(ctx) {
  const sr = ctx.sampleRate, n = Math.round(sr * 0.22); const b = ctx.createBuffer(2, n, sr);
  const L = b.getChannelData(0), R = b.getChannelData(1); let ph = 0, seed = 7;
  const rnd = () => { seed = (seed * 16807) % 2147483647; return seed / 2147483647; };
  for (let i = 0; i < n; i++) { const t = i / sr; const f = 300 + 1100 * Math.exp(-t / 0.018); ph += 2 * Math.PI * f / sr;
    const env = Math.min(1, t / 0.002) * Math.exp(-t / 0.045); const click = (rnd() * 2 - 1) * Math.exp(-t / 0.0015) * 0.35;
    const v = Math.max(-1, Math.min(1, 0.85 * Math.sin(ph) * env + click)) * 0.79; L[i] = v; R[i] = v; }
  return b;
}
export async function mixSound(fileBuf, a, b, voiceBuf, opts = {}) {   // opts: { baseGain, layers: [[ArrayBuffer, gain], ...] }
  const OAC = window.OfflineAudioContext || window.webkitOfflineAudioContext;
  if (!OAC || typeof window.AudioEncoder !== 'function' || typeof window.AudioData !== 'function') return null;
  const sr = 48000; let cfg = { codec: 'mp4a.40.2', sampleRate: sr, numberOfChannels: 2, bitrate: 192000 }; let kind = 'aac';
  const okA = async (c) => { try { return (await window.AudioEncoder.isConfigSupported(c)).supported; } catch (e) { return false; } };
  if (!(await okA(cfg))) { cfg = { codec: 'opus', sampleRate: sr, numberOfChannels: 2, bitrate: 192000 }; kind = 'opus'; if (!(await okA(cfg))) return null; }   // AAC on iPhones; Opus elsewhere
  const dur = Math.max(0.2, b - a);
  const dec = new OAC(2, sr, sr);
  let src; try { src = await dec.decodeAudioData(fileBuf.slice(0)); } catch (e) { return null; }
  let voice = null; if (voiceBuf) { try { voice = await dec.decodeAudioData(voiceBuf.slice(0)); } catch (e) { voice = null; } }
  const off = new OAC(2, Math.ceil(dur * sr), sr);
  const s = off.createBufferSource(); s.buffer = src; const g = off.createGain();
  const bg = off.createGain(); bg.gain.value = opts.baseGain ?? 1; s.connect(bg); bg.connect(g);
  for (const [lb, lgain] of opts.layers || []) {   // e.g. the swapped-in AI voice: dips under the follow line like the clip
    try { const ab = await dec.decodeAudioData(lb.slice(0)); const ls = off.createBufferSource(); ls.buffer = ab; const lg = off.createGain(); lg.gain.value = lgain;
      ls.connect(lg); lg.connect(g); ls.start(0, Math.max(0, a)); } catch (e) { /* skip a layer this phone can't read */ }
  }
  const lim = off.createDynamicsCompressor();   // a safety limiter on the mix
  lim.threshold.value = -2; lim.knee.value = 0; lim.ratio.value = 20; lim.attack.value = 0.002; lim.release.value = 0.12;
  g.connect(lim); lim.connect(off.destination);
  const G = g.gain; const p0 = Math.max(0, POP_AT - 0.06), p1 = POP_AT + 0.45;
  G.setValueAtTime(1, 0); G.setValueAtTime(1, p0); G.linearRampToValueAtTime(0.7, p0 + 0.06);
  if (voice) {
    const v0 = SAY_AT - 0.08, v1 = SAY_AT + voice.duration + 0.05;
    G.linearRampToValueAtTime(SAY_DUCK, v0 + 0.12); G.setValueAtTime(SAY_DUCK, v1); G.linearRampToValueAtTime(1, v1 + 0.3);
    const vs = off.createBufferSource(); vs.buffer = voice; const vg = off.createGain(); vg.gain.value = SAY_GAIN; vs.connect(vg); vg.connect(lim); vs.start(SAY_AT);
  } else { G.setValueAtTime(0.7, p1 - 0.18); G.linearRampToValueAtTime(1, p1); }
  const ps = off.createBufferSource(); ps.buffer = popBuffer(off); const pg = off.createGain(); pg.gain.value = 0.85; ps.connect(pg); pg.connect(lim); ps.start(POP_AT);
  s.start(0, Math.max(0, a), dur);
  const out = await off.startRendering();
  const chunks = []; let err = null;
  const enc = new window.AudioEncoder({ output: (c, m) => chunks.push([c, m]), error: (e) => { err = e; } });
  enc.configure(cfg);
  const N = 1024, L = out.getChannelData(0), R = out.getChannelData(1);
  for (let i = 0; i < out.length && !err; i += N) {
    const n = Math.min(N, out.length - i); const data = new Float32Array(n * 2); data.set(L.subarray(i, i + n), 0); data.set(R.subarray(i, i + n), n);
    for (let k = 0; k < data.length; k++) { const v = data[k]; if (v > 0.98) data[k] = 0.98; else if (v < -0.98) data[k] = -0.98; }   // never clips
    const ad = new window.AudioData({ format: 'f32-planar', sampleRate: sr, numberOfFrames: n, numberOfChannels: 2, timestamp: Math.round(i / sr * 1e6), data });
    enc.encode(ad); ad.close();
    if (enc.encodeQueueSize > 30) await new Promise((r) => setTimeout(r, 0));
  }
  await enc.flush(); enc.close();
  if (err || !chunks.length) return null;
  return { chunks, sr, kind };
}

/**
 * src: URL of the no-caption copy.  a, b: the part to keep (seconds in that video).
 * hook: canvas/image of the hook box (1080-wide units) or null; hookY: its top (px).  ctaY: centre of the pop-up.
 * pbar: draw the progress bar.  onPct(0..1).  Returns a Blob (video/mp4).
 * compose(g, frame, w, h): draw the LAYOUT yourself from a source frame (the live editor's full footage: webcam +
 * game, reaction, full screen...) onto the 1080x1920 canvas - without it the frame is drawn full size (no-caption copy).
 */
// ---- speed: the footage is downloaded ONCE per visit (the editor starts it when it opens), mp4box is loaded early,
// and what this phone's video chip supports is asked once, not on every save
const BUF = new Map();   // footage link -> Promise<ArrayBuffer> (last 3)
export function prefetch(src) {
  if (!src) return null;
  if (!BUF.has(src)) {
    const p = fetch(src).then((r) => { if (!r.ok) throw new Error('footage ' + r.status); return r.arrayBuffer(); });
    p.catch(() => BUF.delete(src));
    BUF.set(src, p);
    while (BUF.size > 3) BUF.delete(BUF.keys().next().value);
  }
  return BUF.get(src);
}
let MP4 = null;
export function warm(src) {   // call when an editor opens: everything the next save needs starts now
  if (!MP4) MP4 = loadScript(new URL('./mp4box.all.js', import.meta.url).href).catch((e) => { MP4 = null; throw e; });
  if (src) prefetch(src);
  return MP4;
}
const SUP = new Map();   // config key -> supported config (or null)
async function encSupported(c) {
  const k = JSON.stringify(c);
  if (!SUP.has(k)) { let ok = false; try { ok = (await window.VideoEncoder.isConfigSupported(c)).supported; } catch (e) { ok = false; } SUP.set(k, ok); }
  return SUP.get(k);
}
async function decSupported(c) {
  const k = 'd' + JSON.stringify({ ...c, description: undefined });
  if (!SUP.has(k)) { let ok = false; try { ok = (await window.VideoDecoder.isConfigSupported(c)).supported; } catch (e) { ok = false; } SUP.set(k, ok); }
  return SUP.get(k);
}
const waitQ = (codec) => new Promise((r) => {   // wake as soon as the chip takes the next frame (no busy polling)
  let done = false; const go = () => { if (!done) { done = true; r(); } };
  try { codec.addEventListener('dequeue', go, { once: true }); } catch (e) { /* older phones */ }
  setTimeout(go, 3);
});

export async function phoneRender({ src, a = 0, b = Infinity, hook = null, hookY = 0, ctaY = 800, pbar = false, onPct = () => {}, compose = null, fps = 30, voice = undefined }) {
  if (!supported()) throw new Error('this phone can\'t make videos in the browser');
  const [, cached] = await Promise.all([warm(), prefetch(src)]);
  const buf = cached.slice(0);   // a copy: the cached footage stays clean for the next save
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

  // ---- encoder: the phone's video chip, highest quality it takes - 60 fps footage stays 60 fps (High profile,
  // level 4.2, ~22 Mbps), else 30 fps at ~16 Mbps. Falls back step by step so every phone gets the best it can do.
  const srcFps = vs.length > 1 ? (vs.length - 1) / Math.max(0.1, (vs[vs.length - 1].cts - vs[0].cts) / vs[0].timescale) : 30;
  const tries = [];
  if (fps === 'auto' ? srcFps >= 50 : fps >= 50) for (const codec of ['avc1.64002a', 'avc1.640033', 'avc1.4d002a']) tries.push({ codec, fr: 60, br: 22_000_000 });
  for (const codec of ['avc1.640028', 'avc1.4d0028', 'avc1.42e028', 'avc1.640033']) tries.push({ codec, fr: 30, br: 16_000_000 });
  let cfg = null, FPS = 30;
  for (const t of tries) {
    const c = { codec: t.codec, width: W, height: H, bitrate: t.br, framerate: t.fr, avc: { format: 'avc' }, hardwareAcceleration: 'prefer-hardware' };
    if (await encSupported(c)) { cfg = c; FPS = t.fr; break; }
    delete c.hardwareAcceleration; if (await encSupported(c)) { cfg = c; FPS = t.fr; break; }
  }
  if (!cfg) throw new Error('no H.264 encoder on this phone');
  const target = new ArrayBufferTarget();
  // sound with the pop + spoken follow line (voice: the line's audio file; undefined = copy the sound as is)
  let mixed = null;   // made on the side WHILE the picture is made (was: before it)
  const mixP = voice !== undefined && at ? mixSound(buf, a, b, voice).catch(() => null) : Promise.resolve(null);

  let aCfg = null;
  if (at) {
    const esds = file.getTrackById(at.id).mdia.minf.stbl.stsd.entries[0].esds;
    const dsi = esds && findTag(esds.esd, 5);
    aCfg = { codec: at.codec || 'mp4a.40.2', sampleRate: at.audio.sample_rate, numberOfChannels: at.audio.channel_count,
             description: dsi ? new Uint8Array(dsi.data) : undefined };
  }
  // the picture's chunks wait here; the file is put together once the sound mix (made alongside) is known
  const vChunks = [];
  let failed = null;
  const enc = new window.VideoEncoder({ output: (chunk, meta) => vChunks.push([chunk, meta]), error: (e) => { failed = e; } });
  enc.configure(cfg);

  // ---- drawing surface
  const cv = typeof OffscreenCanvas === 'function' ? new OffscreenCanvas(W, H) : Object.assign(document.createElement('canvas'), { width: W, height: H });
  const g = cv.getContext('2d');
  let n = 0, lastT = -1;
  const total = Math.max(0.1, b - a);
  const dec = new window.VideoDecoder({
    output: (frame) => {
      const t = frame.timestamp / 1e6; const frameDur = frame.duration;
      if (t < a - 0.001 || t >= b || failed) { frame.close(); return; }
      const rt = t - a;
      if (lastT >= 0 && rt < lastT + 1 / FPS - 0.004) { frame.close(); return; }   // only when the phone can't do 60: 60 -> 30 fps
      lastT = rt;
      try {
        if (compose) compose(g, frame, frame.displayWidth || frame.codedWidth, frame.displayHeight || frame.codedHeight, rt);
        else g.drawImage(frame, 0, 0, W, H);
      } catch (e) { failed = e; }
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
  if (!(await decSupported(dcfg))) { delete dcfg.hardwareAcceleration; if (!(await decSupported(dcfg))) throw new Error('this phone can\'t read ' + vt.codec); }
  dec.configure(dcfg);
  const ts = vs[0].timescale;
  let first = 0;   // decode from the last keyframe at or before the start
  for (let i = 0; i < vs.length; i++) { if (vs[i].is_sync && (vs[i].cts - vShift) / ts <= a + 0.001) first = i; }
  for (let i = first; i < vs.length; i++) {
    const s = vs[i];
    if ((s.dts - vShift) / ts > b + 0.6) break;
    while ((dec.decodeQueueSize > 10 || enc.encodeQueueSize > 12) && !failed) await waitQ(enc.encodeQueueSize > 12 ? enc : dec);
    if (failed) break;
    dec.decode(new window.EncodedVideoChunk({ type: s.is_sync ? 'key' : 'delta', timestamp: Math.round((s.cts - vShift) / ts * 1e6),
      duration: Math.round(s.duration / ts * 1e6), data: s.data }));
  }
  if (failed) throw new Error('video decode/encode failed: ' + (failed.message || failed));
  await dec.flush(); await enc.flush();
  dec.close(); enc.close();
  if (failed) throw failed;
  if (!n) throw new Error('no frames came out');
  // ---- sound: the mix made alongside (pop + follow line), else copied as is (no quality loss)
  mixed = await mixP;   // null = no mix on this phone: the clip's own sound is copied instead (never a silent fail)
  const muxer = new Muxer({ target, video: { codec: 'avc', width: W, height: H, frameRate: FPS },
    audio: mixed ? { codec: mixed.kind || 'aac', sampleRate: mixed.sr, numberOfChannels: 2 }
      : aCfg ? { codec: 'aac', sampleRate: aCfg.sampleRate, numberOfChannels: aCfg.numberOfChannels } : undefined,
    fastStart: 'in-memory', firstTimestampBehavior: 'offset' });
  for (const [c, m] of vChunks) muxer.addVideoChunk(c, m);
  vChunks.length = 0;
  if (mixed) {   // (some phones leave out the AAC setup bytes: AAC-LC 48 kHz stereo = 0x11 0x90)
    let first = true;
    for (const [c, m] of mixed.chunks) {
      let meta = m;
      if (first) { const dc = (m && m.decoderConfig) || {}; if (!dc.description && mixed.kind !== 'opus') meta = { decoderConfig: { codec: 'mp4a.40.2', sampleRate: mixed.sr, numberOfChannels: 2, description: new Uint8Array([0x11, 0x90]) } }; first = false; }
      muxer.addAudioChunk(c, meta);
    }
  }
  else if (aCfg && as.length) {
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

/**
 * Instant voice swap (< 1 s): the clip's finished picture is COPIED as is (no re-encode) and only the sound is new:
 * the clip's voice-free sound (voice pack "game") + the AI voice you picked reading the same lines at the same
 * moments + the pop and follow line, like every clip. video/game/voice: links (fetched once, then cached).
 */
export async function voiceSwap({ video, game, voice = null, follow = null, onPct = () => {} }) {
  if (!supported()) throw new Error('this phone can\'t make videos in the browser');
  const [, vb, gb, vcb] = await Promise.all([warm(), video instanceof Blob ? video.arrayBuffer() : prefetch(video), prefetch(game), voice ? prefetch(voice) : null]);
  onPct(0.2);
  const buf = vb.slice(0); buf.fileStart = 0;
  const file = window.MP4Box.createFile(); let info = null;
  file.onReady = (i) => { info = i; }; file.appendBuffer(buf); file.flush();
  if (!info || !info.videoTracks[0]) throw new Error('could not read the video');
  const vt = info.videoTracks[0]; const vs = [];
  file.onSamples = (id, user, samples) => { vs.push(...samples); };
  file.setExtractionOptions(vt.id, 'v', { nbSamples: 1e9 }); file.start(); file.flush();
  const trak = file.getTrackById(vt.id); const entry = trak.mdia.minf.stbl.stsd.entries[0]; const box = entry.avcC || entry.hvcC;
  const ds = new window.DataStream(undefined, 0, window.DataStream.BIG_ENDIAN); box.write(ds);
  const description = new Uint8Array(ds.buffer, 8);
  const e0 = trak.edts && trak.edts.elst && trak.edts.elst.entries && trak.edts.elst.entries[0]; const shift = e0 && e0.media_time > 0 ? e0.media_time : 0;
  const ts = vs[0].timescale; const last = vs[vs.length - 1];
  const dur = (last.cts + last.duration - shift) / ts;
  const mixed = await mixSound(gb, 0, dur, follow, { baseGain: 0.85, layers: vcb ? [[vcb, 1.6]] : [] });
  if (!mixed) throw new Error('this phone can\'t mix sound');
  onPct(0.8);
  const target = new ArrayBufferTarget();
  const W = vt.video.width, H = vt.video.height;
  const fps = Math.round(vs.length / Math.max(0.1, dur));
  const muxer = new Muxer({ target, video: { codec: 'avc', width: W, height: H, frameRate: fps },
    audio: { codec: mixed.kind || 'aac', sampleRate: mixed.sr, numberOfChannels: 2 }, fastStart: 'in-memory', firstTimestampBehavior: 'offset' });
  let first = true;
  for (const smp of vs) {
    const pts = (smp.cts - shift) / ts, dts = (smp.dts - shift) / ts;
    muxer.addVideoChunkRaw(new Uint8Array(smp.data), smp.is_sync ? 'key' : 'delta', Math.round(pts * 1e6), Math.round(smp.duration / ts * 1e6),
      first ? { decoderConfig: { codec: vt.codec, codedWidth: W, codedHeight: H, description } } : undefined, Math.round((pts - dts) * 1e6));
    first = false;
  }
  let fa = true;
  for (const [c, m] of mixed.chunks) {
    let meta = m;
    if (fa) { const dc = (m && m.decoderConfig) || {}; if (!dc.description && mixed.kind !== 'opus') meta = { decoderConfig: { codec: 'mp4a.40.2', sampleRate: mixed.sr, numberOfChannels: 2, description: new Uint8Array([0x11, 0x90]) } }; fa = false; }
    muxer.addAudioChunk(c, meta);
  }
  muxer.finalize(); onPct(1);
  return new Blob([target.buffer], { type: 'video/mp4' });
}
