// Cloudflare Pages function for the site's videos (only paths listed in _routes.json come here: /v/* and files
// over 25 MB). Two jobs:
//  1. iPhones only play a video if the server answers "byte-range" requests (206). Cloudflare's plain file
//     hosting always sends the whole file, so videos go through here and get proper 206 answers.
//  2. Files over Cloudflare's 25 MB limit live as GitHub release assets (release "site-big"); they're passed
//     through from there with the right type.
const BIG = 'https://github.com/inballin15/Gtaclip-cloud/releases/download/site-big/';

function typeOf(p) { return p.endsWith('.jpg') ? 'image/jpeg' : p.endsWith('.png') ? 'image/png' : 'video/mp4'; }

function parseRange(h, size) {
  const m = /^bytes=(\d*)-(\d*)$/.exec((h || '').trim());
  if (!m) return null;
  let a, b;
  if (m[1] === '') { const n = +m[2]; if (!n) return null; a = Math.max(0, size - n); b = size - 1; }
  else { a = +m[1]; b = m[2] === '' ? size - 1 : Math.min(+m[2], size - 1); }
  return a <= b && a < size ? [a, b] : 'bad';
}

async function slice(full, a, b, size, ct, etag) {   // fallback: cut the bytes out ourselves
  const want = b - a + 1;
  const { readable, writable } = new FixedLengthStream(want);
  (async () => {
    const w = writable.getWriter(); const rd = full.body.getReader();
    let pos = 0, left = want;
    try {
      while (left > 0) {
        const { value, done } = await rd.read(); if (done) break;
        const end = pos + value.length;
        if (end > a) { const s = Math.max(0, a - pos); const part = value.subarray(s, Math.min(value.length, s + left)); await w.write(part); left -= part.length; }
        pos = end;
      }
      rd.cancel().catch(() => {}); await w.close();
    } catch (e) { w.abort(e).catch(() => {}); }
  })();
  return new Response(readable, { status: 206, headers: { 'content-type': ct, 'content-range': `bytes ${a}-${b}/${size}`,
    'accept-ranges': 'bytes', 'cache-control': 'public, max-age=300', ...(etag ? { etag } : {}) } });
}

export async function onRequest({ request, env }) {
  const url = new URL(request.url);
  const path = decodeURIComponent(url.pathname);
  const range = request.headers.get('range');
  const ct = typeOf(path);

  // 1) a normal site file
  const key = new Request(url.origin + url.pathname, { method: 'GET' });
  const head = await env.ASSETS.fetch(new Request(key.url, { method: 'HEAD' }));
  // (a missing file comes back as the site's index.html, so a text/html answer means "not a site file")
  if (head.status === 200 && !(head.headers.get('content-type') || '').includes('text/html')) {
    const etag = head.headers.get('etag') || '';
    const ckey = new Request(url.origin + url.pathname + '?etag=' + encodeURIComponent(etag));
    const cache = caches.default;
    let hit = null;
    try { hit = await cache.match(range ? new Request(ckey.url, { headers: { range } }) : ckey); } catch (e) { /* no cache here */ }
    if (hit) { const r = new Response(hit.body, hit); r.headers.set('accept-ranges', 'bytes'); r.headers.set('content-type', ct); return r; }
    const full = await env.ASSETS.fetch(key);
    if (full.status !== 200) return full;
    const size = +full.headers.get('content-length') || 0;
    const rr = range && size ? parseRange(range, size) : null;
    if (rr === 'bad') return new Response(null, { status: 416, headers: { 'content-range': `bytes */${size}` } });
    const body = new Response(full.body, { headers: { 'content-type': ct, 'accept-ranges': 'bytes', 'cache-control': 'public, max-age=86400', ...(etag ? { etag } : {}), ...(size ? { 'content-length': String(size) } : {}) } });
    if (!rr) {   // whole file: send it, keep a copy in the cache for the range answers
      try { await cache.put(ckey, body.clone()); } catch (e) { /* */ }
      if (request.method === 'HEAD') return new Response(null, { headers: body.headers });
      return body;
    }
    try {
      await cache.put(ckey, body.clone());
      const got = await cache.match(new Request(ckey.url, { headers: { range } }));
      if (got && got.status === 206) { const r = new Response(got.body, got); r.headers.set('content-type', ct); r.headers.set('accept-ranges', 'bytes'); return r; }
    } catch (e) { /* the cache isn't available - cut it ourselves */ }
    const again = await env.ASSETS.fetch(key);
    return slice(again, rr[0], rr[1], size, ct, etag);
  }

  // 2) a file over 25 MB: from the GitHub release
  const name = path.replace(/^\/+/, '').replace(/\//g, '__');
  const h = new Headers(); if (range) h.set('range', range);
  const r = await fetch(BIG + encodeURIComponent(name), { headers: h, redirect: 'follow', cf: { cacheTtl: 3600, cacheEverything: true } });
  if (!r.ok && r.status !== 206) return new Response('not found', { status: 404 });
  const out = new Headers();
  for (const k of ['content-length', 'content-range', 'etag', 'last-modified']) { const v = r.headers.get(k); if (v) out.set(k, v); }
  out.set('content-type', ct); out.set('accept-ranges', 'bytes'); out.set('access-control-allow-origin', '*');
  out.set('cache-control', 'public, max-age=600');
  return new Response(request.method === 'HEAD' ? null : r.body, { status: r.status, headers: out });
}
