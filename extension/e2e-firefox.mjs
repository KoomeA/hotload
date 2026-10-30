// Loads the Firefox build into a real Firefox (temporary add-on, driven over Marionette) and runs it against a throwaway
// Hotload stack (private redis, API, worker; real archive.org media). Needs redis-server, the Python venv and Firefox:
//   FIREFOX=/path/to/firefox HL_BIN=/path/to/venv/bin node extension/e2e-firefox.mjs
// Marionette is spoken directly (Firefox ships it), so there is no driver binary to download.
import { execFileSync, spawn } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { build } from './build.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FIREFOX = process.env.FIREFOX ?? 'firefox';
const HL_BIN = process.env.HL_BIN ?? path.resolve(HERE, '..', '.venv', 'bin');
const PORT = 8101, REDIS = 6391, MARIONETTE = 2829;
const BASE = `http://localhost:${PORT}`;
const GECKO_ID = 'hotload-e2e@example.org';
const MEDIA = 'https://archive.org/details/BigBuckBunny_124';
const results = [];
const ok = (c, m) => { if (!c) throw new Error(m); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const step = async (name, fn) => {
  try { await fn(); results.push(['PASS', name]); } catch (e) { results.push(['FAIL', `${name}: ${String(e.message).split('\n')[0]}`]); }
};

// --- a minimal Marionette client: packets are "<byte length>:<json>"; commands [0, id, name, params] -> [1, id, error, result]
class Marionette {
  constructor(sock) {
    this.sock = sock; this.buf = Buffer.alloc(0); this.next = 1; this.waiting = new Map(); this.hello = null;
    sock.on('data', (d) => { this.buf = Buffer.concat([this.buf, d]); this.drain(); });
  }
  drain() {
    for (;;) {
      const colon = this.buf.indexOf(':');
      if (colon < 0) return;
      const len = Number(this.buf.subarray(0, colon).toString());
      if (this.buf.length < colon + 1 + len) return;
      const msg = JSON.parse(this.buf.subarray(colon + 1, colon + 1 + len).toString('utf8'));
      this.buf = this.buf.subarray(colon + 1 + len);
      if (!Array.isArray(msg)) { this.hello = msg; continue; }
      const [, id, error, result] = msg;
      const w = this.waiting.get(id);
      this.waiting.delete(id);
      if (w) error ? w.reject(new Error(`${error.error}: ${error.message}`)) : w.resolve(result);
    }
  }
  send(name, params = {}) {
    const id = this.next++;
    const body = JSON.stringify([0, id, name, params]);
    this.sock.write(`${Buffer.byteLength(body)}:${body}`);
    return new Promise((resolve, reject) => this.waiting.set(id, { resolve, reject }));
  }
  static async connect(port, tries = 120) {
    for (let i = 0; i < tries; i++) {
      try {
        const sock = await new Promise((res, rej) => { const s = net.connect(port, '127.0.0.1', () => res(s)); s.on('error', rej); });
        const m = new Marionette(sock);
        for (let j = 0; j < 40 && !m.hello; j++) await sleep(100);
        if (m.hello) return m;
      } catch { /* not up yet */ }
      await sleep(500);
    }
    throw new Error('Firefox did not offer Marionette');
  }
}

// --- throwaway stack
const data = fs.mkdtempSync(path.join(os.tmpdir(), 'hl-ffx-'));
const env = { ...process.env, HOTLOAD_DATA_DIR: path.join(data, 'hl'), HOTLOAD_REDIS_URL: `redis://localhost:${REDIS}/0`, HOTLOAD_CANARY_ENABLED: 'false',
  HOTLOAD_RATE_LIMIT_PER_MINUTE: '500', HOTLOAD_WEB_DIR: path.resolve(HERE, '..', 'frontend', 'build') };
const procs = [];
const run = (cmd, args, opts = {}) => { const p = spawn(cmd, args, { stdio: 'ignore', env, ...opts }); procs.push(p); return p; };
const cleanup = () => procs.forEach((p) => p.kill());
process.on('exit', cleanup);

run('redis-server', ['--port', String(REDIS), '--save', '', '--dir', data]);
await sleep(1000);
run(path.join(HL_BIN, 'uvicorn'), ['hotload.api.app:app', '--port', String(PORT)]);
run(path.join(HL_BIN, 'arq'), ['hotload.worker.WorkerSettings']);
for (let i = 0; i < 60; i++) { if (await fetch(`${BASE}/healthz`).then((r) => r.ok).catch(() => false)) break; await sleep(500); }

const post = (p, body, headers = {}) => fetch(BASE + p, { method: 'POST', headers: { 'content-type': 'application/json', ...headers }, body: JSON.stringify(body) });
const reg = await post('/api/v1/auth/register', { email: `ffx${Date.now()}@example.com`, password: 'correct horse battery staple' });
const cookie = reg.headers.get('set-cookie').split(';')[0];
const KEY = (await (await post('/api/v1/me/keys', { name: 'firefox' }, { cookie })).json()).key;
if (!KEY) throw new Error('could not create an API key');

// --- build and start Firefox
const built = build(path.join(data, 'dist'), GECKO_ID);
const profile = path.join(data, 'profile');
fs.mkdirSync(profile);
fs.writeFileSync(path.join(profile, 'user.js'), [
  `user_pref("marionette.port", ${MARIONETTE});`, 'user_pref("browser.shell.checkDefaultBrowser", false);',
  'user_pref("datareporting.policy.dataSubmissionEnabled", false);', 'user_pref("app.update.enabled", false);',
  'user_pref("browser.startup.homepage_override.mstone", "ignore");', 'user_pref("startup.homepage_welcome_url", "about:blank");'].join('\n'));
run(FIREFOX, ['--marionette', '--headless', '--no-remote', '--profile', profile, 'about:blank'], { env: { ...env, MOZ_HEADLESS: '1', MOZ_REMOTE_ALLOW_SYSTEM_ACCESS: '1' } });
const m = await Marionette.connect(MARIONETTE);
await m.send('WebDriver:NewSession', { capabilities: {} });
const installed = await m.send('Addon:Install', { path: built.firefox.dir, temporary: true });
await m.send('Marionette:SetContext', { value: 'chrome' });
const uuids = JSON.parse((await m.send('WebDriver:ExecuteScript', { script: "return Services.prefs.getStringPref('extensions.webextensions.uuids')", args: [] })).value);
await m.send('Marionette:SetContext', { value: 'content' });
const UUID = uuids[GECKO_ID];

// --- page helpers (everything runs inside the extension page, so a click is a real DOM click)
const exec = async (script, args = []) => (await m.send('WebDriver:ExecuteScript', { script, args })).value;
const execAsync = async (script, args = []) => (await m.send('WebDriver:ExecuteAsyncScript', { script, args })).value;
const go = (url) => m.send('WebDriver:Navigate', { url });
const ext = (page, query = '') => go(`moz-extension://${UUID}/${page}${query}`);
const text = () => exec('return document.body.innerText');
const waitText = async (re, ms = 15000) => {
  const end = Date.now() + ms;
  let last = '';
  while (Date.now() < end) { last = await text().catch(() => ''); if (re.test(last)) return last; await sleep(250); }
  throw new Error(`text ${re} never appeared; page said: ${last.slice(0, 120).replace(/\n/g, ' | ')}`);
};
const fill = (sel, value) => exec('const e = document.querySelector(arguments[0]); e.value = arguments[1]; e.dispatchEvent(new Event("input", { bubbles: true }));', [sel, value]);
const clickButton = (label) => exec('const b = [...document.querySelectorAll("button")].find((x) => x.textContent.trim() === arguments[0]); if (!b) throw new Error("no button " + arguments[0]); b.click();', [label]);
const tabQuery = (url, title = 'Big Buck Bunny') => `?url=${encodeURIComponent(url)}&title=${encodeURIComponent(title)}`;

await step('the Firefox package installs as a temporary add-on with its declared id and minimal permissions', async () => {
  ok(installed.value === GECKO_ID, `installed as ${installed.value}`);
  ok(UUID, 'Firefox gave the add-on no internal address');
  const man = JSON.parse(fs.readFileSync(path.join(built.firefox.dir, 'manifest.json'), 'utf8'));
  ok(JSON.stringify([...man.permissions].sort()) === JSON.stringify(['activeTab', 'contextMenus', 'storage']), 'permissions');
  ok(!man.host_permissions && !man.content_scripts && !man.background.service_worker, 'no host access, no content scripts, no service worker');
});

await step('the background script ran: its install handler created the "Open in Hotload" context menu', async () => {
  await ext('popup.html', tabQuery(MEDIA));
  // Creating a menu with an id that already exists is an error, so "already exists" proves background.js executed
  // (a missing or broken script leaves no menu, and the create succeeds).
  const message = await execAsync(`const done = arguments[arguments.length - 1];
    browser.contextMenus.create({ id: 'hotload-open', title: 'probe' }, () => done(browser.runtime.lastError ? browser.runtime.lastError.message : 'created'));`);
  ok(/already exists/i.test(String(message)), `menu probe said: ${message}`);
});

await step('without a server the popup asks for setup', async () => {
  await ext('popup.html', tabQuery(MEDIA));
  await waitText(/Set your Hotload server first\./);
});

await step('settings: rejects insecure servers, tests the connection, saves', async () => {
  await ext('options.html');
  await fill('#server', 'http://hotload.example');
  await clickButton('Save');
  await waitText(/valid https address/);
  await fill('#server', BASE + '/some/path');
  await fill('#key', 'hl_000000000000_wrongwrongwrong');
  await clickButton('Test connection');
  await waitText(/Invalid API key|rejected/);
  await fill('#key', KEY);
  await clickButton('Test connection');
  await waitText(/Connected\. Your API key works\./);
  await clickButton('Save');
  await waitText(/Saved\./);
  ok((await exec('return document.querySelector("#server").value')) === BASE, 'server normalised to its origin');
});

await step('popup: non-web pages are refused', async () => {
  await ext('popup.html', tabQuery('about:addons'));
  await waitText(/This page can't be downloaded\./);
});

await step('popup: "Open in Hotload" opens a new tab on the web app with the link fetched', async () => {
  await ext('popup.html', tabQuery(MEDIA));
  await waitText(/archive\.org/);
  const before = await m.send('WebDriver:GetWindowHandles');
  await clickButton('Open in Hotload');
  let handles = before;
  for (let i = 0; i < 40 && handles.length <= before.length; i++) { await sleep(250); handles = await m.send('WebDriver:GetWindowHandles'); }
  ok(handles.length > before.length, 'no new tab opened');
  await m.send('WebDriver:SwitchToWindow', { handle: handles.find((h) => !before.includes(h)) });
  for (let i = 0; i < 40; i++) { if ((await m.send('WebDriver:GetCurrentURL')).value?.startsWith(BASE)) break; await sleep(250); }
  ok((await m.send('WebDriver:GetCurrentURL')).value.startsWith(BASE), 'the new tab is not on the Hotload server');
  await waitText(/Big Buck Bunny/, 60000);
  await m.send('WebDriver:SwitchToWindow', { handle: before[0] });
});

await step('popup: one-click MP3 download works cross-origin with the API key', async () => {
  await ext('popup.html', tabQuery(MEDIA));
  await waitText(/archive\.org/);
  await clickButton('Download');
  const end = Date.now() + 170000;
  let href = null;
  while (Date.now() < end && !href) { href = await exec('const a = document.querySelector("#save a, a#save"); return a && a.href ? a.href : null').catch(() => null); if (!href) await sleep(1000); }
  ok(href, `no save link; page said: ${(await text()).slice(0, 120)}`);
  ok(href.startsWith(`${BASE}/api/v1/jobs/`) && href.includes('/file?token='), `link ${href}`);
  const bytes = (await (await fetch(href)).arrayBuffer()).byteLength;
  ok(bytes > 100000, `downloaded ${bytes} bytes`);
});

await step('popup: a wrong key shows a clear error', async () => {
  await ext('options.html');
  await fill('#key', 'hl_000000000000_wrongwrongwrong');
  await clickButton('Save');
  await waitText(/Saved\./);
  await ext('popup.html', tabQuery(MEDIA));
  await waitText(/archive\.org/);
  await clickButton('Download');
  await waitText(/Invalid API key/);
});

await m.send('Marionette:Quit').catch(() => {});
cleanup();
for (const [s, n] of results) console.log(`${s}  ${n}`);
process.exit(results.some(([s]) => s === 'FAIL') ? 1 : 0);
