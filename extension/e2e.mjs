// Loads the unpacked extension into Chromium and drives it against a running Hotload stack (API + worker + Redis).
// Usage: BASE=http://localhost:8000 node extension/e2e.mjs   (needs playwright; uses real archive.org media)
import { createRequire } from 'node:module';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const { chromium } = createRequire(import.meta.url)('playwright');
const BASE = process.env.BASE ?? 'http://localhost:8000';
const EXT = path.dirname(fileURLToPath(import.meta.url));
const MEDIA = 'https://archive.org/details/BigBuckBunny_124';
const results = [];
const ok = (c, m) => { if (!c) throw new Error(m); };
const step = async (name, fn) => {
  try { await fn(); results.push(['PASS', name]); } catch (e) { results.push(['FAIL', `${name}: ${String(e.message).split('\n')[0]}`]); }
};

// An account with an API key, created through the public API like any customer would.
const post = (p, body, headers = {}) => fetch(BASE + p, { method: 'POST', headers: { 'content-type': 'application/json', ...headers }, body: JSON.stringify(body) });
const email = `ext${Date.now()}@example.com`;
const reg = await post('/api/v1/auth/register', { email, password: 'correct horse battery staple' });
const cookie = reg.headers.get('set-cookie').split(';')[0];
const KEY = (await (await post('/api/v1/me/keys', { name: 'extension' }, { cookie })).json()).key;
if (!KEY) throw new Error('could not create an API key');

const userData = fs.mkdtempSync(path.join(os.tmpdir(), 'hl-ext-'));
const ctx = await chromium.launchPersistentContext(userData, {
  channel: 'chromium',
  headless: true,
  args: [`--disable-extensions-except=${EXT}`, `--load-extension=${EXT}`]
});
let [sw] = ctx.serviceWorkers();
if (!sw) sw = await ctx.waitForEvent('serviceworker', { timeout: 20000 });
const id = new URL(sw.url()).host;
const popup = async (query = '') => {
  const p = await ctx.newPage();
  await p.goto(`chrome-extension://${id}/popup.html${query}`);
  return p;
};
const tabQuery = (url, title = 'Big Buck Bunny') => `?url=${encodeURIComponent(url)}&title=${encodeURIComponent(title)}`;

await step('extension loads with the declared minimal permissions', async () => {
  const m = JSON.parse(fs.readFileSync(path.join(EXT, 'manifest.json'), 'utf8'));
  ok(m.manifest_version === 3 && sw.url().endsWith('/background.js'), 'MV3 service worker');
  ok(JSON.stringify([...m.permissions].sort()) === JSON.stringify(['activeTab', 'contextMenus', 'storage']), 'permissions');
  ok(!m.host_permissions && !m.optional_host_permissions && !m.content_scripts, 'no host access, no content scripts');
});

await step('without a server the popup asks for setup', async () => {
  const p = await popup(tabQuery(MEDIA));
  await p.getByText('Set your Hotload server first.').waitFor();
  await p.close();
});

await step('settings: rejects insecure servers, tests the connection, saves', async () => {
  const o = await ctx.newPage();
  await o.goto(`chrome-extension://${id}/options.html`);
  await o.getByLabel('Your Hotload server').fill('http://hotload.example');
  await o.getByRole('button', { name: 'Save' }).click();
  await o.getByText(/valid https address/).waitFor();
  await o.getByLabel('Your Hotload server').fill(BASE + '/some/path');
  await o.getByLabel(/API key/).fill('hl_000000000000_wrongwrongwrong');
  await o.getByRole('button', { name: 'Test connection' }).click();
  await o.getByText(/Invalid API key|rejected/).waitFor({ timeout: 15000 });
  await o.getByLabel(/API key/).fill(KEY);
  await o.getByRole('button', { name: 'Test connection' }).click();
  await o.getByText('Connected. Your API key works.').waitFor({ timeout: 15000 });
  await o.getByRole('button', { name: 'Save' }).click();
  await o.getByText('Saved.').waitFor();
  ok((await o.getByLabel('Your Hotload server').inputValue()) === BASE, 'server normalised to its origin');
  await o.close();
});

await step('popup: non-web pages are refused', async () => {
  const p = await popup(tabQuery('chrome://extensions'));
  await p.getByText("This page can't be downloaded.").waitFor();
  await p.close();
});

await step('popup: "Open in Hotload" opens the web app with the link fetched', async () => {
  const p = await popup(tabQuery(MEDIA));
  await p.getByText('archive.org').waitFor();
  const [tab] = await Promise.all([ctx.waitForEvent('page'), p.getByRole('button', { name: 'Open in Hotload' }).click()]);
  await tab.waitForLoadState();
  ok(new URL(tab.url()).origin === BASE || tab.url().startsWith(BASE), `opened ${tab.url()}`);
  await tab.getByRole('heading', { name: 'Big Buck Bunny' }).waitFor({ timeout: 60000 });
  await tab.close();
  await p.close();
});

await step('popup: one-click MP3 download works cross-origin with the API key', async () => {
  const p = await popup(tabQuery(MEDIA));
  await p.getByLabel('Quick download').selectOption('mp3');
  await p.getByRole('button', { name: 'Download', exact: true }).click();
  const save = p.getByRole('link', { name: /^Save / });
  await save.waitFor({ timeout: 170000 });
  const href = await save.getAttribute('href');
  ok(href.startsWith(BASE + '/api/v1/jobs/') && href.includes('/file?token='), `link ${href}`);
  const bytes = (await (await fetch(href)).arrayBuffer()).byteLength;
  ok(bytes > 100000, `downloaded ${bytes} bytes`);
  await p.close();
});

await step('popup: a revoked/wrong key shows a clear error', async () => {
  const o = await ctx.newPage();
  await o.goto(`chrome-extension://${id}/options.html`);
  await o.getByLabel(/API key/).fill('hl_000000000000_wrongwrongwrong');
  await o.getByRole('button', { name: 'Save' }).click();
  await o.getByText('Saved.').waitFor();
  await o.close();
  const p = await popup(tabQuery(MEDIA));
  await p.getByRole('button', { name: 'Download', exact: true }).click();
  await p.getByText(/Invalid API key/).waitFor({ timeout: 15000 });
  await p.close();
});

await ctx.close();
for (const [s, n] of results) console.log(`${s}  ${n}`);
process.exit(results.some(([s]) => s === 'FAIL') ? 1 : 0);
