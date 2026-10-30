// Email verification and password reset, in a real browser, against a throwaway API instance whose mail goes to a local
// SMTP sink, and a private redis-server on port 6390. Needs redis-server and the Python venv. Usage:
//   HL_BIN=/path/to/venv/bin node e2e/m5.mjs      (HL_BIN defaults to ../.venv/bin)
import { execFileSync, spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';

const { chromium } = createRequire(import.meta.url)('playwright');
const HL_BIN = process.env.HL_BIN ?? path.resolve('..', '.venv', 'bin');
const PORT = 8100;
const SMTP_PORT = 2525;
const BASE = `http://localhost:${PORT}`;
const email = `m5${Date.now()}@example.com`;
const OLD = 'correct horse battery staple';
const NEW = 'a completely different passphrase';
const results = [];
const problems = [];
const ok = (c, m) => { if (!c) throw new Error(m); };
const step = async (name, fn) => {
  try { await fn(); results.push(['PASS', name]); } catch (e) {
    results.push(['FAIL', `${name}: ${String(e.message).split('\n')[0]}`]);
    await page.screenshot({ path: path.join(data, `fail-${results.length}.png`) }).catch(() => {});
  }
};

// --- a minimal SMTP sink: accepts anything, keeps the message text
const mails = [];
const sink = net.createServer((sock) => {
  let data = '', buf = '', inData = false;
  sock.write('220 sink ESMTP\r\n');
  sock.on('data', (chunk) => {
    buf += chunk.toString();
    let i;
    while ((i = buf.indexOf('\r\n')) >= 0) {
      const line = buf.slice(0, i);
      buf = buf.slice(i + 2);
      if (inData) {
        if (line === '.') { inData = false; mails.push(data); data = ''; sock.write('250 queued\r\n'); } else data += line.replace(/^\.\./, '.') + '\n';
      } else if (/^(EHLO|HELO)/i.test(line)) sock.write('250 sink\r\n');
      else if (/^DATA/i.test(line)) { inData = true; sock.write('354 go\r\n'); }
      else if (/^QUIT/i.test(line)) { sock.write('221 bye\r\n'); sock.end(); }
      else sock.write('250 ok\r\n');
    }
  });
  sock.on('error', () => {});
});
await new Promise((r) => sink.listen(SMTP_PORT, '127.0.0.1', r));

const data = fs.mkdtempSync(path.join(os.tmpdir(), 'hl-m5-'));
const redis = spawn('redis-server', ['--port', '6390', '--save', '', '--dir', data], { stdio: 'ignore' });
await new Promise((r) => setTimeout(r, 1000));
const api = spawn(path.join(HL_BIN, 'uvicorn'), ['hotload.api.app:app', '--port', String(PORT)], {
  env: { ...process.env, HOTLOAD_DATA_DIR: data, HOTLOAD_REDIS_URL: 'redis://localhost:6390/0', HOTLOAD_CANARY_ENABLED: 'false',
    HOTLOAD_WEB_DIR: path.resolve('build'), HOTLOAD_PUBLIC_URL: BASE, HOTLOAD_SMTP_HOST: '127.0.0.1', HOTLOAD_SMTP_PORT: String(SMTP_PORT),
    HOTLOAD_SMTP_SECURITY: 'none', HOTLOAD_COOKIE_KEY: execFileSync(path.join(HL_BIN, 'python'), ['-m', 'hotload.admin', 'cookie-key']).toString().trim(), HOTLOAD_MAIL_FROM: 'Hotload <no-reply@hotload.test>' },
  stdio: 'ignore',
});
console.log('artifacts:', data);
const stop = () => { api.kill(); redis.kill(); sink.close(); };
for (let i = 0; i < 60; i++) {
  if (await fetch(`${BASE}/healthz`).then((r) => r.ok).catch(() => false)) break;
  await new Promise((r) => setTimeout(r, 500));
}

// Typing before the page hydrates is lost (Svelte resets the field), so wait for the page's first API call to finish.
const ready = (url) => Promise.all([page.waitForResponse((r) => r.url().includes('/api/v1/auth/providers')), page.goto(url)]);
const waitMail = async (n) => {
  for (let i = 0; i < 40 && mails.length < n; i++) await new Promise((r) => setTimeout(r, 250));
  ok(mails.length >= n, `expected ${n} mail(s), got ${mails.length}`);
  return mails[n - 1];
};
const linkIn = (text, kind) => {
  // Long lines travel quoted-printable (soft line breaks, =3D for "="); a mail client decodes them, so do the same.
  const decoded = text.replace(/=\r?\n/g, '').replace(/=([0-9A-F]{2})/g, (_, h) => String.fromCharCode(parseInt(h, 16)));
  const m = decoded.match(new RegExp(`${BASE}/account/${kind}/#token=[\\w.\\-]+`));
  ok(m, `no ${kind} link in the message`);
  return m[0];
};

const browser = await chromium.launch({ executablePath: process.env.CHROMIUM || undefined });
const ctx = await browser.newContext({ locale: 'en-US', viewport: { width: 1000, height: 900 } });
const page = await ctx.newPage();
page.on('console', (m) => { if (['error', 'warning'].includes(m.type()) && !/401|Unauthorized/.test(m.text())) problems.push(`console ${m.type()}: ${m.text()}`); });
page.on('response', (r) => { if (r.url().includes('/api/v1/auth/')) console.log('  <-', r.status(), r.request().method(), r.url().replace(BASE, '')); });
page.on('pageerror', (e) => problems.push(`pageerror: ${e.message}`));

await step('sign-up sends a link; the account shows it is unconfirmed; opening the link confirms it', async () => {
  await ready(`${BASE}/account/`);
  await page.getByRole('tab', { name: 'Create account' }).click();
  await page.getByLabel('Email').fill(email);
  await page.getByLabel('Password').fill(OLD);
  await page.getByRole('button', { name: 'Create account' }).last().click();
  await page.getByText('Confirm your email address. We sent a link to').waitFor({ timeout: 15000 });
  const link = linkIn(await waitMail(1), 'verify');
  const fresh = await ctx.newPage(); // as if opened from the mailbox in another tab
  await fresh.goto(link);
  await fresh.getByText('Your email address is confirmed.').waitFor({ timeout: 15000 });
  ok(!fresh.url().includes('token'), `the token is still in the address bar: ${fresh.url()}`);
  await fresh.close();
  await page.goto(`${BASE}/account/`);
  await page.getByText(email).first().waitFor();
  ok((await page.getByText('Confirm your email address.').count()) === 0, 'the banner is still showing');
});

await step('forgot password: same message for any address; mail only for the real one', async () => {
  await page.getByRole('button', { name: 'Sign out' }).click();
  await Promise.all([page.waitForResponse((r) => r.url().includes('/api/v1/auth/providers')), page.getByRole('link', { name: 'Forgot your password?' }).click()]);
  await page.getByLabel('Email').fill(`nobody${Date.now()}@example.com`);
  await page.getByRole('button', { name: 'Email me a reset link' }).click();
  await page.getByText('If that address has an account, a reset link is on its way.').waitFor();
  await new Promise((r) => setTimeout(r, 1500));
  ok(mails.length === 1, `an unknown address got mail (${mails.length} total)`);
  await page.getByLabel('Email').fill(email);
  await page.getByRole('button', { name: 'Email me a reset link' }).click();
  await waitMail(2);
});

let resetLink;
await step('the reset link opens the form, leaves no token in the address bar, and sets a new password', async () => {
  resetLink = linkIn(mails[1], 'reset');
  await page.goto(resetLink);
  await page.getByRole('heading', { name: 'Choose a new password' }).waitFor();
  ok(!page.url().includes('token'), `the token is still in the address bar: ${page.url()}`);
  await page.getByLabel('Password').fill(NEW);
  await page.getByRole('button', { name: 'Save new password' }).click();
  await page.getByText('Your password was changed. You are signed in.').waitFor({ timeout: 15000 });
  await page.goto(`${BASE}/account/`);
  await page.getByText(email).first().waitFor();
});

await step('the old password no longer works, the new one does, and the link cannot be used twice', async () => {
  const post = (p, body) => fetch(`${BASE}${p}`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
  ok((await post('/api/v1/auth/login', { email, password: OLD })).status === 401, 'old password still works');
  ok((await post('/api/v1/auth/login', { email, password: NEW })).status === 200, 'new password rejected');
  const token = new URLSearchParams(resetLink.split('#')[1]).get('token');
  ok((await post('/api/v1/auth/password/reset', { token, password: 'yet another passphrase' })).status === 400, 'link was reusable');
});

await step('a used link shows a readable error in the page', async () => {
  const fresh = await ctx.newPage();
  await fresh.goto(resetLink);
  await fresh.getByRole('heading', { name: 'Choose a new password' }).waitFor();
  await fresh.getByLabel('Password').fill('one more passphrase here');
  await fresh.getByRole('button', { name: 'Save new password' }).click();
  await fresh.getByText('This link is invalid or has expired').waitFor({ timeout: 15000 });
  await fresh.close();
});

await browser.close();
stop();
let failed = 0;
for (const [s, n] of results) { console.log(`${s}  ${n}`); if (s === 'FAIL') failed++; }
console.log(problems.length ? `\nProblems:\n${problems.join('\n')}` : '\nNo unexpected browser console errors.');
process.exit(failed || problems.length ? 1 : 0);
