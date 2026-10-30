const URL_RE = /https?:\/\/[^\s<>"'`]+/gi;

/** All http(s) URLs found in free text (share sheets often put the link inside a sentence). */
export function urlsFromText(text) {
  return [...new Set((String(text ?? '').match(URL_RE) ?? []).map((u) => u.replace(/[).,;!?]+$/, '')))];
}

/** Pick the link out of a Web Share Target / ?url= query: prefer `url`, then `text`, then `title`. */
export function sharedUrl(params) {
  for (const key of ['url', 'text', 'title']) {
    const found = urlsFromText(params.get(key));
    if (found.length) return found[0];
  }
  return null;
}

/** Heuristic: is this link a playlist page (rather than one video that merely sits inside a playlist)? */
export function looksLikePlaylist(url) {
  if (/\/playlist\b|\/sets\/|\/album\//i.test(url)) return true;
  return /[?&]list=/i.test(url) && !/[?&]v=/i.test(url);
}

/** A single video opened from inside a playlist (has both v= and list=): offer to load the whole list. */
export function isVideoInPlaylist(url) {
  return /[?&]list=/i.test(url) && /[?&]v=/i.test(url);
}
