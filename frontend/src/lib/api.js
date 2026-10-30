export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

/** JSON request against the Hotload API (same origin, cookies included). */
export async function api(path, { method = 'GET', body } = {}) {
  const res = await fetch(path, {
    method,
    credentials: 'same-origin',
    headers: body === undefined ? {} : { 'content-type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body)
  });
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(res.status, messageFrom(data, res.status));
  return data;
}

/** FastAPI returns `detail` as a string, or a list of validation errors. */
export function messageFrom(data, status) {
  const d = data?.detail;
  if (typeof d === 'string') return d;
  if (Array.isArray(d) && d.length) return String(d[0].msg ?? '').replace(/^Value error, /, '') || `Error ${status}`;
  return `Error ${status}`;
}

/** Subscribe to live job updates (server-sent events). Returns a function that stops listening. */
export function watchJob(id, onUpdate, onGone) {
  const es = new EventSource(`/api/v1/jobs/${id}/events`);
  es.onmessage = (ev) => {
    const job = JSON.parse(ev.data);
    onUpdate(job);
    if (job.state === 'done' || job.state === 'error') es.close();
  };
  es.onerror = () => {
    es.close();
    onGone?.();
  };
  return () => es.close();
}
