/** Parse "90", "1:30" or "1:02:03" into seconds. Returns null for anything else. */
export function parseTime(text) {
  const s = String(text ?? '').trim();
  if (!s) return null;
  if (!/^\d+(\.\d+)?$|^(\d+:)?\d{1,2}:\d{1,2}(\.\d+)?$/.test(s)) return null;
  const parts = s.split(':').map(Number);
  if (parts.length > 1 && parts.slice(1).some((p) => p >= 60)) return null;
  return parts.reduce((acc, p) => acc * 60 + p, 0);
}

/** 65 -> "1:05", 3725 -> "1:02:05" */
export function formatTime(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const ss = String(s).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`;
}

/** Keep a [start, end] selection valid inside [0, duration] with at least `min` seconds between handles. */
export function clampRange(start, end, duration, min = 1) {
  const d = Math.max(0, duration);
  let s = Math.min(Math.max(0, start), Math.max(0, d - min));
  let e = Math.min(Math.max(end, s + min), d);
  s = Math.min(s, e - min);
  return [Math.max(0, s), e];
}
