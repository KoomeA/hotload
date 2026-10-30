// Pure helpers shared by the popup, options page and background worker. No browser APIs here, so they are unit tested
// with `node --test extension/lib.test.mjs`.

export const PRESETS = [
  ['mp3', 'MP3 audio'],
  ['best_mp4', 'Best video (MP4)'],
  ['720p', '720p video'],
  ['m4a', 'M4A audio']
];

const LOCAL = new Set(['localhost', '127.0.0.1', '[::1]']);

/** "hotload.example/" -> "https://hotload.example". Plain http is only accepted for localhost. Returns null if unusable. */
export function normalizeServer(input) {
  let text = String(input ?? '').trim();
  if (!text) return null;
  if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(text)) text = `https://${text}`;
  let u;
  try {
    u = new URL(text);
  } catch {
    return null;
  }
  if (u.username || u.password) return null;
  if (u.protocol === 'http:' && !LOCAL.has(u.hostname)) return null; // API keys must not travel in the clear
  if (u.protocol !== 'https:' && u.protocol !== 'http:') return null;
  return u.origin;
}

/** Only real web pages can be downloaded; browser-internal pages and other schemes cannot. */
export function isDownloadableUrl(url) {
  try {
    const u = new URL(url);
    return (u.protocol === 'http:' || u.protocol === 'https:') && !!u.hostname;
  } catch {
    return false;
  }
}

/** The Hotload page that pre-fills and fetches this link (the same ?url= entry point as the web share target). */
export function openUrl(server, pageUrl) {
  return `${server}/?url=${encodeURIComponent(pageUrl)}`;
}

/** fetch() arguments for creating a job with an API key. */
export function jobRequest(server, apiKey, pageUrl, preset) {
  return [
    `${server}/api/v1/jobs`,
    {
      method: 'POST',
      headers: { 'content-type': 'application/json', authorization: `Bearer ${apiKey}` },
      body: JSON.stringify({ url: pageUrl, preset })
    }
  ];
}

export function authHeaders(apiKey) {
  return { authorization: `Bearer ${apiKey}` };
}

/** Server-provided download paths are relative ("/api/..."). Anything else is ignored rather than followed. */
export function downloadLink(server, path) {
  if (typeof path !== 'string' || !path.startsWith('/') || path.startsWith('//')) return null;
  return `${server}${path}`;
}

export function errorText(status, body) {
  const d = body && body.detail;
  if (typeof d === 'string') return d;
  if (Array.isArray(d) && d[0]?.msg) return String(d[0].msg).replace(/^Value error, /, '');
  if (status === 401) return 'The server rejected your API key.';
  if (status === 429) return 'Too many requests. Try again in a minute.';
  return `Request failed (${status}).`;
}

export function shortHost(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, '');
  } catch {
    return '';
  }
}
