// Cloudflare Pages: files bigger than 25 MB can't be hosted directly, so they live as GitHub release assets
// (release "site-big") and this little function passes them through with the right type, so iPhones can play them.
// Only the paths listed in _routes.json (made at deploy time) ever reach this code.
export async function onRequest({ request }) {
  const url = new URL(request.url);
  const name = decodeURIComponent(url.pathname).replace(/^\/+/, '').replace(/\//g, '__');
  const src = `https://github.com/inballin15/Gtaclip-cloud/releases/download/site-big/${encodeURIComponent(name)}`;
  const h = new Headers();
  const range = request.headers.get('range');
  if (range) h.set('range', range);
  const r = await fetch(src, { headers: h, redirect: 'follow', cf: { cacheTtl: 3600, cacheEverything: true } });
  if (!r.ok && r.status !== 206) return new Response('not found', { status: 404 });
  const out = new Headers();
  for (const k of ['content-length', 'content-range', 'etag', 'last-modified']) { const v = r.headers.get(k); if (v) out.set(k, v); }
  out.set('content-type', name.endsWith('.jpg') ? 'image/jpeg' : 'video/mp4');
  out.set('accept-ranges', 'bytes');
  out.set('access-control-allow-origin', '*');
  out.set('cache-control', 'public, max-age=600');
  return new Response(request.method === 'HEAD' ? null : r.body, { status: r.status, headers: out });
}
