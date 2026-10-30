import { PRESETS, downloadLink, errorText, isDownloadableUrl, jobRequest, openUrl, shortHost, authHeaders } from './lib.js';

const $ = (id) => document.getElementById(id);
const show = (id, on = true) => ($(id).hidden = !on);

async function currentTab() {
  // `?url=&title=` lets the popup be opened as a normal tab (used by the end-to-end test); otherwise use the active tab.
  const q = new URLSearchParams(location.search);
  if (q.get('url')) return { url: q.get('url'), title: q.get('title') || '' };
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab ?? {};
}

async function main() {
  const { server, key, preset = 'mp3' } = await chrome.storage.local.get(['server', 'key', 'preset']);
  const tab = await currentTab();
  if (!server) return show('setup');
  if (!isDownloadableUrl(tab.url)) return show('bad');

  show('ready');
  $('title').textContent = tab.title || tab.url;
  $('host').textContent = shortHost(tab.url);
  $('open').addEventListener('click', () => chrome.tabs.create({ url: openUrl(server, tab.url) }));

  if (!key) return show('hint');
  show('quick');
  for (const [value, label] of PRESETS) $('preset').add(new Option(label, value, false, value === preset));
  $('go').addEventListener('click', () => download(server, key, tab.url));
}

async function download(server, key, pageUrl) {
  $('go').disabled = true;
  show('save', false);
  $('status').textContent = 'Starting…';
  try {
    const [url, init] = jobRequest(server, key, pageUrl, $('preset').value);
    const res = await fetch(url, init);
    const body = await res.json().catch(() => null);
    if (!res.ok) throw new Error(errorText(res.status, body));
    await watch(server, key, body.id);
  } catch (e) {
    $('status').textContent = e instanceof TypeError ? 'Cannot reach your server.' : e.message;
    show('bar', false);
    $('go').disabled = false;
  }
}

async function watch(server, key, id) {
  show('bar');
  for (let i = 0; i < 400; i++) {
    const res = await fetch(`${server}/api/v1/jobs/${id}`, { headers: authHeaders(key) });
    const job = await res.json();
    $('bar').value = job.progress ?? 0;
    $('status').textContent = job.state === 'error' ? job.message : job.state === 'done' ? 'Ready' : job.message || job.state;
    if (job.state === 'done') {
      const link = downloadLink(server, job.download_url);
      if (link) {
        $('save').href = link;
        $('save').textContent = `Save ${job.filename ?? 'file'}`;
        show('save');
      }
      break;
    }
    if (job.state === 'error') break;
    await new Promise((r) => setTimeout(r, 1500));
  }
  show('bar', false);
  $('go').disabled = false;
}

$('open-options').addEventListener('click', () => chrome.runtime.openOptionsPage());
main();
