// M3 end-to-end test: plans, checkout, API keys, report form, admin takedown. Needs a stack started with plans enforced
// and Paystack pointed at e2e/fake_paystack.py (see the README). Usage: BASE=http://localhost:8001 PYTHON=python3 node e2e/m3.mjs
import { execFileSync } from 'node:child_process';
import { createRequire } from 'node:module';

const { chromium } = createRequire(import.meta.url)('playwright');
const BASE = process.env.BASE ?? 'http://localhost:8001';
const PYTHON = process.env.PYTHON ?? 'python3';
const PW = 'correct horse battery staple';
const stamp = Date.now();
const buyer = `buyer${stamp}@example.com`;
const boss = `boss${stamp}@example.com`;
const badHost = `blocked${stamp}.example.org`; // unique per run so the test is repeatable against a used database
const results = [];
const problems = [];
const ok = (c, m) => { if (!c) throw new Error(m); };
const step = async (name, fn) => {
  try { await fn(); results.push(['PASS', name]); } catch (e) { results.push(['FAIL', `${name}: ${String(e.message).split('\n')[0]}`]); }
};

const browser = await chromium.launch({ executablePath: process.env.CHROMIUM || undefined });
const newPage = async (opts = {}) => {
  const ctx = await browser.newContext({ locale: 'en-US', viewport: { width: 1000, height: 900 }, ...opts });
  const page = await ctx.newPage();
  page.on('console', (m) => { if (['error', 'warning'].includes(m.type())) problems.push(`console ${m.type()}: ${m.text()}`); });
  page.on('pageerror', (e) => problems.push(`pageerror: ${e.message}`));
  return page;
};
async function register(page, email) {
  await page.goto(BASE + '/account/');
  await page.getByRole('tab', { name: 'Create account' }).click();
  await page.getByLabel('Email').fill(email);
  await page.getByLabel('Password').fill(PW);
  await page.getByRole('button', { name: 'Create account' }).last().click();
  await page.getByText(email).first().waitFor({ timeout: 15000 });
}

const user = await newPage();

await step('signed-in free user sees enforced plan limits', async () => {
  await register(user, buyer);
  await user.getByText('Plan: free').waitFor();
  await user.getByText(/0 of 10 downloads used/).waitFor();
  await user.getByText('Up to 20 minutes per file').waitFor();
});

await step('plans list shows the configured payment method and exact prices', async () => {
  await user.getByRole('heading', { name: 'Upgrade' }).waitFor();
  await user.getByRole('button', { name: 'Pay $9.99 with card or M-Pesa' }).waitFor();
  await user.getByRole('button', { name: 'Pay $239.99 with card or M-Pesa' }).waitFor();
  ok((await user.getByRole('button', { name: /with crypto/ }).count()) === 0, 'crypto is not configured, must not be offered');
});

await step('buying Premium: redirect to provider, return, plan becomes Premium', async () => {
  const buttons = user.getByRole('button', { name: 'Pay $9.99 with card or M-Pesa' });
  await buttons.first().click();
  await user.waitForURL(/127\.0\.0\.1:\d+\/pay\//, { timeout: 15000 });
  await user.getByRole('link', { name: 'Pay now' }).click();
  await user.waitForURL(/\/account\/\?payment=/, { timeout: 15000 }).catch(() => {});
  await user.getByText(/Payment received\. Your plan is now premium\./).waitFor({ timeout: 30000 });
  await user.getByText(/Active until/).waitFor();
  await user.getByText('Plan: premium').waitFor();
  await user.getByText('Up to 180 minutes per file').waitFor();
  ok(!user.url().includes('payment='), 'payment params are removed from the address bar');
  await user.getByText(/Premium, 30 days · \$9\.99/).waitFor();
});

await step('API keys are locked for Premium, unlocked after an admin grants Pro', async () => {
  await user.getByText('API keys are part of the Pro plan.').waitFor();
  const adminPage = await newPage();
  await register(adminPage, boss);
  execFileSync(PYTHON, ['-m', 'hotload.admin', 'grant', boss], { env: { ...process.env } });
  await adminPage.goto(BASE + '/admin/');
  await adminPage.getByRole('heading', { name: 'Overview' }).waitFor({ timeout: 15000 });
  await adminPage.getByLabel('Account email').fill(buyer);
  await adminPage.getByLabel('Plan').selectOption('pro');
  await adminPage.getByRole('button', { name: 'Apply' }).click();
  await adminPage.getByText(new RegExp(`${buyer} is now pro`)).waitFor();
  await user.reload();
  await user.getByRole('heading', { name: 'API keys' }).waitFor();
  await user.getByLabel('Key name').waitFor({ timeout: 15000 });
  globalThis.adminPage = adminPage;
});

await step('creating a key shows it once; it works as a bearer token; revoking stops it', async () => {
  await user.getByLabel('Key name').fill('ci');
  await user.getByRole('button', { name: 'Create key' }).click();
  await user.getByText('Copy your new key now').waitFor();
  const key = await user.getByLabel('API key', { exact: true }).inputValue();
  const secret = await user.getByLabel('Webhook signing secret').inputValue();
  ok(/^hl_[0-9a-f]{12}_[\w-]{40,}$/.test(key) && secret.length >= 32, 'key format');
  const api = (await newPage()).request;
  const job = await api.post(BASE + '/api/v1/jobs', { headers: { authorization: `Bearer ${key}` }, data: { url: 'https://archive.org/details/BigBuckBunny_124' } });
  ok(job.status() === 202, `job with key: ${job.status()}`);
  await user.reload();
  ok((await user.getByText('Copy your new key now').count()) === 0, 'the key is never shown again');
  await user.getByText(/hl_[0-9a-f]{12}_…/).waitFor();
  await user.getByText(/1 downloads/).waitFor();
  await user.getByRole('button', { name: 'Revoke' }).click();
  await user.getByText('No keys yet.').waitFor();
  const after = await api.post(BASE + '/api/v1/jobs', { headers: { authorization: `Bearer ${key}` }, data: { url: 'https://archive.org/details/BigBuckBunny_124' } });
  ok(after.status() === 401, `revoked key: ${after.status()}`);
});

await step('public report form is accepted and validates input', async () => {
  const anon = await newPage();
  await anon.goto(BASE + '/report/');
  await anon.getByRole('heading', { name: 'Report content' }).waitFor();
  await anon.getByLabel('Link to the content').fill(`https://${badHost}/watch/42`);
  await anon.getByLabel('What is the problem?').selectOption('copyright');
  await anon.getByLabel('Your email').fill('owner@studio.example');
  await anon.getByLabel(/Details/).fill('This is my work.');
  await anon.getByRole('button', { name: 'Send report' }).click();
  await anon.getByText(/Your report was received \(reference/).waitFor();
});

await step('admin sees the report, blocks the host in one step, and extraction is refused', async () => {
  const a = globalThis.adminPage;
  await a.goto(BASE + '/admin/');
  await a.getByText(`${badHost}/watch/42`).waitFor({ timeout: 15000 });
  await a.getByLabel('Block', { exact: true }).check();
  await a.getByRole('button', { name: 'Action' }).click();
  await a.getByText('Report actioned.').waitFor();
  await a.getByText(badHost).first().waitFor();
  const anon = (await newPage()).request;
  const r = await anon.post(BASE + '/api/v1/jobs', { data: { url: `https://cdn.${badHost}/anything` } });
  ok(r.status() === 451, `blocked host (and subdomain): ${r.status()}`);
  await a.locator('li', { hasText: badHost }).getByRole('button', { name: 'Remove' }).click();
  await a.locator('li', { hasText: badHost }).waitFor({ state: 'detached' });
  const again = await anon.post(BASE + '/api/v1/jobs', { data: { url: `https://cdn.${badHost}/anything` } });
  ok(again.status() !== 451, `after removing the rule it must no longer be blocked: ${again.status()}`); // 400: the fake host does not resolve
});

await step('non-admins cannot use the admin console', async () => {
  await user.goto(BASE + '/admin/');
  await user.getByText('This account is not an administrator.').waitFor({ timeout: 15000 });
});

await step('mobile: account page with plans has no horizontal overflow', async () => {
  const m = await newPage({ viewport: { width: 375, height: 800 }, isMobile: true, hasTouch: true });
  await m.goto(BASE + '/account/');
  await m.getByRole('tab', { name: 'Sign in' }).waitFor();
  await m.getByLabel('Email').fill(buyer);
  await m.getByLabel('Password').fill(PW);
  await m.getByRole('button', { name: 'Sign in' }).last().click();
  await m.getByRole('heading', { name: 'Upgrade' }).waitFor({ timeout: 15000 });
  const overflow = await m.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  ok(overflow <= 0, `overflow ${overflow}px`);
  await m.screenshot({ path: '/tmp/claude-0/m3-mobile.png', fullPage: true });
});

await browser.close();
for (const [s, n] of results) console.log(`${s}  ${n}`);
const noise = problems.filter((p) => !/Failed to load resource.*(40[0-9]|451|429|503)/.test(p));
console.log(noise.length ? `\nBrowser console problems:\n${noise.join('\n')}` : '\nNo unexpected browser console errors or CSP violations.');
process.exit(results.some(([s]) => s === 'FAIL') ? 1 : 0);
