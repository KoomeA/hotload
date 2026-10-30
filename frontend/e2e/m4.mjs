// M4 UI end-to-end test: saved cookies, live recording and SponsorBlock controls, tools page. Needs a stack with
// HOTLOAD_COOKIE_KEY set and plans NOT enforced. Usage: BASE=http://localhost:8000 node e2e/m4.mjs
import { createRequire } from 'node:module';

const { chromium } = createRequire(import.meta.url)('playwright');
const BASE = process.env.BASE ?? 'http://localhost:8000';
const SECRET = 'M4SUPERSECRETVALUE';
const email = `m4${Date.now()}@example.com`;
const results = [];
const problems = [];
const ok = (c, m) => { if (!c) throw new Error(m); };
const step = async (name, fn) => {
  try { await fn(); results.push(['PASS', name]); } catch (e) { results.push(['FAIL', `${name}: ${String(e.message).split('\n')[0]}`]); }
};

const browser = await chromium.launch({ executablePath: process.env.CHROMIUM || undefined });
const ctx = await browser.newContext({ locale: 'en-US', viewport: { width: 1000, height: 900 } });
const page = await ctx.newPage();
page.on('console', (m) => { if (['error', 'warning'].includes(m.type())) problems.push(`console ${m.type()}: ${m.text()}`); });
page.on('pageerror', (e) => problems.push(`pageerror: ${e.message}`));

const jar = (domain, value) => `# Netscape HTTP Cookie File\n${domain}\tTRUE\t/\tTRUE\t2000000000\tsid\t${value}\n.unrelated.org\tTRUE\t/\tFALSE\t2000000000\tother\tx\n`;
const fakeExtract = (extra) => ({ id: 'abc', title: 'Mock title', uploader: 'u', duration: 120, thumbnail: null, site: 'Generic',
  age_limit: 0, is_live: false, live_status: 'not_live', heights: [720], has_subtitles: false, subtitles: [], auto_captions: [],
  audio_langs: [], adult: false, age_gate_required: false, ...extra });
const mockExtract = async (extra) => {
  await page.unroute('**/api/v1/extract').catch(() => {});
  await page.route('**/api/v1/extract', (route) => route.fulfill({ json: fakeExtract(extra) }));
};
const captureJob = async () => {
  const seen = { body: null };
  await page.unroute('**/api/v1/jobs').catch(() => {});
  await page.route('**/api/v1/jobs', async (route) => {
    seen.body = JSON.parse(route.request().postData());
    await route.fulfill({ status: 202, json: { id: 'fake', state: 'queued', progress: 0, message: '', preset: 'mp3' } });
  });
  return seen;
};

await step('register, then saved cookies: bad input is explained, good input is scoped and stored', async () => {
  await page.goto(BASE + '/account/');
  await page.getByRole('tab', { name: 'Create account' }).click();
  await page.getByLabel('Email').fill(email);
  await page.getByLabel('Password').fill('correct horse battery staple');
  await page.getByRole('button', { name: 'Create account' }).last().click();
  await page.getByRole('heading', { name: 'Saved cookies' }).waitFor({ timeout: 15000 });
  await page.getByLabel(/^Site/).fill('localhost');
  await page.getByLabel(/Paste your cookies/).fill(jar('.example.com', SECRET));
  await page.getByRole('button', { name: 'Save cookies' }).click();
  await page.getByRole('alert').filter({ hasText: /site name/i }).waitFor();
  await page.getByLabel(/^Site/).fill('example.com');
  await page.getByRole('button', { name: 'Save cookies' }).click();
  await page.getByText('Saved 1 cookies for example.com.').waitFor();
  ok((await page.getByLabel(/Paste your cookies/).inputValue()) === '', 'the pasted cookies are cleared from the form');
  await page.getByText('example.com', { exact: false }).first().waitFor();
  ok(!(await page.content()).includes(SECRET), 'cookie value must never appear in the page');
});

await step('"Use my saved cookies" appears only for the saved site and is sent with the request', async () => {
  await page.goto(BASE + '/');
  await page.getByLabel('Video or audio link').fill('https://other.example.net/v');
  ok((await page.getByLabel('Use my saved cookies for this site').count()) === 0, 'not offered for another site');
  await page.getByLabel('Video or audio link').fill('https://media.example.com/watch/1');
  await page.getByLabel('Use my saved cookies for this site').check();
  const [req] = await Promise.all([
    page.waitForRequest((r) => r.url().endsWith('/api/v1/extract') && r.method() === 'POST'),
    page.getByRole('button', { name: 'Fetch' }).click()
  ]);
  ok(JSON.parse(req.postData()).use_cookies === true, 'extract carries use_cookies');
  await page.getByRole('alert').first().waitFor({ timeout: 30000 }); // example.com has no extractable media: an error is expected
  ok(!(await page.content()).includes(SECRET), 'cookie value never reaches the page');
});

await step('deleting saved cookies removes the option', async () => {
  await page.goto(BASE + '/account/');
  await page.getByRole('button', { name: 'Delete', exact: true }).click();
  await page.getByText('No saved cookies.').waitFor();
  await page.goto(BASE + '/');
  await page.getByLabel('Video or audio link').fill('https://media.example.com/watch/1');
  await page.waitForTimeout(500);
  ok((await page.getByLabel('Use my saved cookies for this site').count()) === 0, 'option gone');
});

await step('live stream: record option defaults on, minutes are sent, trimming is hidden', async () => {
  await mockExtract({ is_live: true, live_status: 'is_live', duration: null });
  const job = await captureJob();
  await page.goto(BASE + '/');
  await page.getByLabel('Video or audio link').fill('https://archive.org/details/live-demo');
  await page.getByRole('button', { name: 'Fetch' }).click();
  await page.getByText('This is live right now.').waitFor();
  const rec = page.getByLabel('Record this live stream');
  ok(await rec.isChecked(), 'record defaults to on for a live stream');
  ok((await page.getByLabel('Download only part of it').count()) === 0, 'no trimming for live');
  await page.getByLabel('Minutes to record').fill('15');
  await page.getByRole('button', { name: 'Download', exact: true }).click();
  await page.waitForTimeout(500);
  ok(job.body.live_minutes === 15 && !('start' in job.body) && !('sponsorblock' in job.body), JSON.stringify(job.body));
});

await step('upcoming stream explains it has not started', async () => {
  await mockExtract({ is_live: true, live_status: 'is_upcoming', duration: null });
  await page.goto(BASE + '/');
  await page.getByLabel('Video or audio link').fill('https://archive.org/details/upcoming-demo');
  await page.getByRole('button', { name: 'Fetch' }).click();
  await page.getByText('This stream has not started yet.').waitFor();
  ok((await page.getByLabel('Record this live stream').count()) === 0, 'cannot record an upcoming stream');
});

await step('SponsorBlock: only for YouTube, sent when chosen, disabled while trimming', async () => {
  await mockExtract({ site: 'Generic' });
  await page.goto(BASE + '/');
  await page.getByLabel('Video or audio link').fill('https://archive.org/details/x');
  await page.getByRole('button', { name: 'Fetch' }).click();
  await page.getByRole('heading', { name: 'Mock title' }).waitFor();
  ok((await page.locator('#sb').count()) === 0, 'not offered for other sites');

  await mockExtract({ site: 'Youtube' });
  const job = await captureJob();
  await page.getByLabel('Video or audio link').fill('https://archive.org/details/y');
  await page.getByRole('button', { name: 'Fetch' }).click();
  await page.locator('#sb').waitFor();
  await page.locator('#sb').selectOption('remove');
  await page.getByLabel('Download only part of it').check();
  ok(await page.locator('#sb').isDisabled(), 'disabled while trimming');
  await page.getByText('Not available while trimming.').waitFor();
  await page.getByLabel('Download only part of it').uncheck();
  await page.getByRole('button', { name: 'Download', exact: true }).click();
  await page.waitForTimeout(500);
  ok(job.body.sponsorblock === 'remove', JSON.stringify(job.body));
});

await step('tools page: server address, drag-only bookmarklet with an encoded target, translations', async () => {
  await page.goto(BASE + '/tools/');
  await page.getByRole('heading', { name: 'Tools', exact: true }).waitFor();
  ok((await page.getByLabel('Your server address').inputValue()) === BASE, 'server address');
  const href = await page.locator('#bookmarklet').getAttribute('href');
  ok(href.startsWith('javascript:') && href.includes(`${BASE}/?url=`) && href.includes('encodeURIComponent(location.href)'), href);
  await page.getByLabel('Language').selectOption('sw');
  await page.getByRole('heading', { name: 'Zana', exact: true }).waitFor();
  await page.getByLabel('Lugha').selectOption('en');
});

await step('mobile: tools and account pages do not overflow', async () => {
  const m = await browser.newContext({ viewport: { width: 375, height: 800 }, isMobile: true, hasTouch: true });
  const p = await m.newPage();
  for (const path of ['/tools/', '/']) {
    await p.goto(BASE + path);
    await p.waitForTimeout(400);
    const o = await p.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    ok(o <= 0, `${path} overflows by ${o}px`);
  }
  await m.close();
});

await browser.close();
for (const [s, n] of results) console.log(`${s}  ${n}`);
const noise = problems.filter((x) => !/Failed to load resource.*(40[0-9]|422|451|429|503)/.test(x) && !/Refused to run the JavaScript URL/.test(x));
console.log(noise.length ? `\nBrowser console problems:\n${noise.join('\n')}` : '\nNo unexpected browser console errors or CSP violations.');
process.exit(results.some(([s]) => s === 'FAIL') ? 1 : 0);
