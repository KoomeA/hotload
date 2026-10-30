import assert from 'node:assert/strict';
import { test } from 'node:test';
import { authHeaders, downloadLink, errorText, isDownloadableUrl, jobRequest, normalizeServer, openUrl, shortHost } from './lib.js';

test('normalizeServer adds https, strips paths, and refuses insecure or odd servers', () => {
  assert.equal(normalizeServer('hotload.example'), 'https://hotload.example');
  assert.equal(normalizeServer(' https://Hotload.example/some/path?x=1 '), 'https://hotload.example');
  assert.equal(normalizeServer('https://hotload.example:8443/'), 'https://hotload.example:8443');
  assert.equal(normalizeServer('http://localhost:8000'), 'http://localhost:8000');
  assert.equal(normalizeServer('http://127.0.0.1:8000/'), 'http://127.0.0.1:8000');
  for (const bad of ['', '   ', null, undefined, 'http://hotload.example', 'ftp://hotload.example', 'javascript:alert(1)',
    'https://user:pw@hotload.example', 'https://', 'not a url']) assert.equal(normalizeServer(bad), null, String(bad));
});

test('isDownloadableUrl accepts web pages only', () => {
  assert.equal(isDownloadableUrl('https://www.youtube.com/watch?v=1'), true);
  assert.equal(isDownloadableUrl('http://example.com/a'), true);
  for (const bad of ['chrome://extensions', 'about:blank', 'file:///etc/passwd', 'javascript:1', 'data:text/html,x',
    'chrome-extension://abc/popup.html', '', 'nonsense', null]) assert.equal(isDownloadableUrl(bad), false, String(bad));
});

test('openUrl encodes the page address so it cannot inject parameters', () => {
  const u = new URL(openUrl('https://h.example', 'https://v.example/watch?v=1&list=2#t=5'));
  assert.equal(u.origin, 'https://h.example');
  assert.equal(u.searchParams.get('url'), 'https://v.example/watch?v=1&list=2#t=5');
  assert.deepEqual([...u.searchParams.keys()], ['url']);
});

test('jobRequest sends a bearer key and a JSON body', () => {
  const [url, init] = jobRequest('https://h.example', 'hl_abc_def', 'https://v.example/x', 'mp3');
  assert.equal(url, 'https://h.example/api/v1/jobs');
  assert.equal(init.method, 'POST');
  assert.equal(init.headers.authorization, 'Bearer hl_abc_def');
  assert.deepEqual(JSON.parse(init.body), { url: 'https://v.example/x', preset: 'mp3' });
  assert.deepEqual(authHeaders('k'), { authorization: 'Bearer k' });
});

test('downloadLink only follows server-relative paths', () => {
  assert.equal(downloadLink('https://h.example', '/api/v1/jobs/1/file?token=t'), 'https://h.example/api/v1/jobs/1/file?token=t');
  for (const bad of ['https://evil.example/x', '//evil.example/x', 'javascript:1', '', null, undefined, 5]) assert.equal(downloadLink('https://h.example', bad), null, String(bad));
});

test('errorText prefers the server message', () => {
  assert.equal(errorText(400, { detail: 'Nope' }), 'Nope');
  assert.equal(errorText(422, { detail: [{ msg: 'Value error, bad' }] }), 'bad');
  assert.match(errorText(401, {}), /API key/);
  assert.match(errorText(429, null), /Too many/);
  assert.equal(errorText(500, null), 'Request failed (500).');
});

test('shortHost', () => {
  assert.equal(shortHost('https://www.example.com/a'), 'example.com');
  assert.equal(shortHost('nonsense'), '');
});
