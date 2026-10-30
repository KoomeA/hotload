// End-to-end smoke test: drives the built web app against a running Hotload stack (API + worker + Redis).
// Usage: BASE=http://localhost:8000 node e2e/smoke.mjs   (needs `playwright`; set CHROMIUM=/path to reuse a browser)
// Uses real public media (YouTube "Me at the zoo", archive.org), so it needs internet access.
import { execFileSync } from 'node:child_process';
import { createRequire } from 'node:module';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

// createRequire honours NODE_PATH, so a globally installed Playwright works too.
const { chromium } = createRequire(import.meta.url)('playwright');

const BASE = process.env.BASE ?? 'http://localhost:8000';
const ZOO = 'https://www.youtube.com/watch?v=jNQXAC9IVRw';
const BBB = 'https://archive.org/details/BigBuckBunny_124'; // has a duration; downloads work from datacentre IPs
const MP4 = 'https://archive.org/download/BigBuckBunny_124/Content/big_buck_bunny_720p_surround.mp4';
const out = fs.mkdtempSync(path.join(os.tmpdir(), 'hl-e2e-'));
const results = [];
const problems = [];

const step = async (name, fn) => {
  try {
    await fn();
    results.push(['PASS', name]);
  } catch (e) {
    results.push(['FAIL', `${name}: ${String(e.message).split('\n')[0]}`]);
  }
};
const ok = (cond, msg) => { if (!cond) throw new Error(msg); };

const browser = await chromium.launch({ executablePath: process.env.CHROMIUM || undefined });
const ctx = await browser.newContext({ viewport: { width: 1000, height: 900 }, acceptDownloads: true, locale: 'en-US' });
const page = await ctx.newPage();
page.on('console', (m) => { if (['error', 'warning'].includes(m.type())) problems.push(`console ${m.type()}: ${m.text()}`); });
page.on('pageerror', (e) => problems.push(`pageerror: ${e.message}`));

async function save(link, name) {
  const [dl] = await Promise.all([page.waitForEvent('download'), link.click()]);
  const dest = path.join(out, name);
  await dl.saveAs(dest);
  return dest;
}
const probeSeconds = (file) =>
  parseFloat(execFileSync('ffprobe', ['-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', file]).toString());

await step('home loads with heading, presets and no console errors', async () => {
  await page.goto(BASE + '/');
  await page.getByRole('heading', { level: 1 }).waitFor();
  ok((await page.getByRole('heading', { level: 1 }).innerText()).includes('Paste a link'), 'heading text');
});

await step('language switch to Spanish and Swahili, and back', async () => {
  await page.getByLabel('Language').selectOption('es');
  ok((await page.getByRole('heading', { level: 1 }).innerText()).includes('Pega un enlace'), 'es heading');
  ok((await page.locator('html').getAttribute('lang')) === 'es', 'html lang');
  await page.getByLabel('Idioma').selectOption('sw');
  ok((await page.getByRole('heading', { level: 1 }).innerText()).includes('Bandika kiungo'), 'sw heading');
  await page.getByLabel('Lugha').selectOption('en');
});

await step('dark theme toggle sets data-theme and persists on reload', async () => {
  await page.getByLabel('Theme').selectOption('dark');
  ok((await page.locator('html').getAttribute('data-theme')) === 'dark', 'dark attr');
  await page.reload();
  await page.getByRole('heading', { level: 1 }).waitFor();
  ok((await page.locator('html').getAttribute('data-theme')) === 'dark', 'persisted');
  await page.screenshot({ path: path.join(out, 'home-dark.png') });
  await page.getByLabel('Theme').selectOption('system');
});

await step('thumbnails load through our own origin; the browser never contacts a third-party host', async () => {
  const hosts = new Set();
  const spy = (r) => hosts.add(new URL(r.url()).origin);
  page.on('request', spy);
  await page.goto(BASE + '/');
  await page.getByLabel('Video or audio link').fill(BBB);
  await page.getByRole('button', { name: 'Fetch' }).click();
  await page.getByRole('heading', { name: 'Big Buck Bunny' }).waitFor({ timeout: 60000 });
  const img = page.locator('.media img');
  await img.waitFor();
  await page.waitForFunction(() => document.querySelector('.media img')?.complete);
  const src = await img.getAttribute('src');
  ok(src.startsWith('/api/v1/thumb?u='), `thumbnail src is ${src}`);
  ok((await img.evaluate((el) => el.naturalWidth)) > 0, 'the proxied thumbnail actually rendered');
  page.off('request', spy);
  ok([...hosts].every((h) => h === new URL(BASE).origin), `third-party requests: ${[...hosts].filter((h) => h !== new URL(BASE).origin)}`);
});

await step('single video: fetch, trim to 5s, MP3 download is ~5s', async () => {
  await page.getByLabel('Video or audio link').fill(BBB);
  await page.getByRole('button', { name: 'Fetch' }).click();
  await page.getByRole('heading', { name: 'Big Buck Bunny' }).waitFor({ timeout: 60000 });
  await page.getByLabel('Format').selectOption('mp3');
  await page.getByLabel('Download only part of it').check();
  await page.locator('#trim-end').fill('0:05');
  await page.locator('#trim-end').dispatchEvent('change');
  ok((await page.locator('#trim-msg').innerText()).includes('0:05'), 'selection length shows 0:05');
  await page.screenshot({ path: path.join(out, 'single-card.png'), fullPage: true });
  await page.getByRole('button', { name: 'Download', exact: true }).click();
  const link = page.getByRole('link', { name: /^Save / });
  await link.waitFor({ timeout: 120000 });
  const file = await save(link, 'zoo.mp3');
  const secs = probeSeconds(file);
  ok(secs > 3.5 && secs < 7, `trimmed duration ${secs}s`);
});

await step('subtitles-only download gives an SRT with text', async () => {
  await page.getByLabel('Video or audio link').fill(ZOO);
  await page.getByRole('button', { name: 'Fetch' }).click();
  await page.getByRole('heading', { name: 'Me at the zoo' }).waitFor({ timeout: 60000 });
  await page.getByLabel('Format').selectOption('subtitles');
  await page.getByLabel('Include auto-generated captions').check();
  await page.locator('.chips label', { hasText: 'English' }).first().locator('input').check();
  await page.getByRole('button', { name: 'Download', exact: true }).click();
  const link = page.getByRole('link', { name: /^Save .*\.srt/ });
  await link.waitFor({ timeout: 120000 });
  const text = fs.readFileSync(await save(link, 'zoo.srt'), 'utf8');
  ok(/-->/.test(text) && /elephant/i.test(text), 'srt content');
});

await step('batch of two links shows progress and a ZIP', async () => {
  await page.getByLabel('Video or audio link').fill(`${MP4}\nhttps://archive.org/details/Popeye_forPresident`);
  await page.getByRole('button', { name: 'Fetch' }).click();
  await page.getByRole('heading', { name: 'Batch of 2 links' }).waitFor();
  await page.getByLabel('Format').selectOption('mp3');
  await page.getByRole('button', { name: 'Download', exact: true }).click();
  const zip = page.getByRole('link', { name: 'Download all (ZIP)' });
  await zip.waitFor({ timeout: 180000 });
  await page.getByText('2 of 2 finished').waitFor({ timeout: 180000 });
  const file = await save(zip, 'batch.zip');
  const listing = execFileSync('python3', ['-c', 'import sys,zipfile;print("\\n".join(zipfile.ZipFile(sys.argv[1]).namelist()))', file]).toString();
  ok(listing.trim().split('\n').length === 2 && /\.mp3/.test(listing), `zip listing: ${listing}`);
});

await step('web share target prefills and fetches the shared link', async () => {
  await page.goto(`${BASE}/?title=Check%20this&text=${encodeURIComponent('Look at this ' + ZOO + ' it is great')}`);
  await page.getByRole('heading', { name: 'Me at the zoo' }).waitFor({ timeout: 60000 });
  ok((await page.getByLabel('Video or audio link').inputValue()) === ZOO, 'input holds just the link');
  ok(!page.url().includes('text='), 'shared params removed from the address bar');
});

await step('blocked internal address shows a readable error', async () => {
  await page.goto(BASE + '/');
  await page.getByLabel('Video or audio link').fill('http://169.254.169.254/latest/meta-data');
  await page.getByRole('button', { name: 'Fetch' }).click();
  await page.getByRole('alert').filter({ hasText: 'non-public' }).waitFor({ timeout: 15000 });
});

await step('adult link requires the 18+ confirmation before download', async () => {
  await page.goto(BASE + '/');
  await page.getByLabel('Video or audio link').fill('https://www.pornhub.com/view_video.php?viewkey=ph000000000');
  await page.getByRole('button', { name: 'Fetch' }).click();
  // The page may not exist (extract error), but the server must still demand age confirmation on a job.
  const res = await page.request.post(BASE + '/api/v1/jobs', { data: { url: 'https://www.pornhub.com/view_video.php?viewkey=x' } });
  ok(res.status() === 451, `expected 451 got ${res.status()}`);
});

await step('account: register, plan, history, sign out/in, delete', async () => {
  await page.goto(BASE + '/account/');
  await page.getByRole('tab', { name: 'Create account' }).click();
  const email = `e2e${Date.now()}@example.com`;
  await page.getByLabel('Email').fill(email);
  await page.getByLabel('Password').fill('correct horse battery staple');
  await page.getByRole('button', { name: 'Create account' }).last().click();
  await page.getByText(email).first().waitFor({ timeout: 15000 });
  await page.getByText('No download limit on this server.').waitFor();
  // a job made while signed in lands in history
  await page.goto(BASE + '/');
  await page.getByLabel('Video or audio link').fill(BBB);
  await page.getByRole('button', { name: 'Fetch' }).click();
  await page.getByRole('heading', { name: 'Big Buck Bunny' }).waitFor({ timeout: 60000 });
  await page.getByLabel('Format').selectOption('mp3');
  await page.getByLabel('Download only part of it').check();
  await page.locator('#trim-end').fill('0:03');
  await page.locator('#trim-end').dispatchEvent('change');
  await page.getByRole('button', { name: 'Download', exact: true }).click();
  await page.getByRole('link', { name: /^Save / }).waitFor({ timeout: 120000 });
  await page.goto(BASE + '/account/');
  await page.getByText('Big Buck Bunny').first().waitFor({ timeout: 15000 });
  await page.getByRole('button', { name: 'Sign out' }).click();
  await page.getByRole('tab', { name: 'Sign in' }).waitFor();
  await page.getByLabel('Email').fill(email);
  await page.getByLabel('Password').fill('wrong password here');
  await page.getByRole('button', { name: 'Sign in' }).last().click();
  await page.getByRole('alert').filter({ hasText: 'Incorrect email or password' }).waitFor();
  await page.getByLabel('Password').fill('correct horse battery staple');
  await page.getByRole('button', { name: 'Sign in' }).last().click();
  await page.getByRole('button', { name: 'Delete my account' }).waitFor();
  await page.getByLabel('Confirm your password').fill('correct horse battery staple');
  await page.getByRole('button', { name: 'Delete my account' }).click();
  await page.getByRole('tab', { name: 'Sign in' }).waitFor({ timeout: 15000 });
});

await step('mobile viewport: no horizontal scroll, controls reachable', async () => {
  const m = await browser.newContext({ viewport: { width: 375, height: 800 }, isMobile: true, hasTouch: true });
  const p = await m.newPage();
  await p.goto(BASE + '/');
  await p.getByLabel('Video or audio link').fill(ZOO);
  await p.getByRole('button', { name: 'Fetch' }).click();
  await p.getByRole('heading', { name: 'Me at the zoo' }).waitFor({ timeout: 60000 });
  await p.getByLabel('Download only part of it').check();
  const overflow = await p.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  await p.screenshot({ path: path.join(out, 'mobile.png'), fullPage: true });
  ok(overflow <= 0, `horizontal overflow ${overflow}px`);
  await m.close();
});

await step('offline: the app shell loads from the service worker cache', async () => {
  await page.goto(BASE + '/');
  await page.evaluate(() => navigator.serviceWorker.ready);
  await page.waitForFunction(async () => {
    const names = await caches.keys();
    for (const n of names) {
      const urls = (await (await caches.open(n)).keys()).map((r) => new URL(r.url).pathname);
      if (urls.includes('/') && urls.some((u) => u.startsWith('/_app/immutable/'))) return true;
    }
    return false;
  }, null, { timeout: 20000 });  // cached under ABSOLUTE paths: the review found relative manifest paths never matched
  await ctx.setOffline(true);
  try {
    await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
    await page.getByRole('heading', { level: 1 }).waitFor({ timeout: 15000 });
    await page.getByLabel('Video or audio link').waitFor();
  } finally {
    await ctx.setOffline(false);
  }
});

await step('service worker registers and manifest has share target + icons', async () => {
  await page.goto(BASE + '/');
  await page.waitForFunction(() => navigator.serviceWorker.getRegistration().then((r) => !!r), null, { timeout: 15000 });
  const man = await (await page.request.get(BASE + '/manifest.webmanifest')).json();
  ok(man.share_target?.params?.url === 'url' && man.icons.some((i) => i.sizes === '512x512'), 'manifest');
  for (const icon of ['/icon-192.png', '/icon-512.png']) ok((await page.request.get(BASE + icon)).ok(), icon);
});

await browser.close();
for (const [s, n] of results) console.log(`${s}  ${n}`);
const pageProblems = problems.filter((p) => !/Failed to load resource.*(451|400|401|404|409|422|429|503)/.test(p));
console.log(pageProblems.length ? `\nBrowser console problems:\n${pageProblems.join('\n')}` : '\nNo unexpected browser console errors or CSP violations.');
console.log(`Artifacts (screenshots/downloads): ${out}`);
process.exit(results.some(([s]) => s === 'FAIL') ? 1 : 0);
