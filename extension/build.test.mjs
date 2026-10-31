import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { DEFAULT_GECKO_ID, build, firefoxManifest } from './build.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const base = JSON.parse(fs.readFileSync(path.join(HERE, 'manifest.json'), 'utf8'));

test('the Chromium manifest is the source manifest, a service worker and nothing Firefox-specific', () => {
  assert.equal(base.manifest_version, 3);
  assert.ok(base.background.service_worker);
  assert.equal(base.browser_specific_settings, undefined);
});

test('the Firefox manifest swaps in an event page, an add-on id and drops Chromium-only keys', () => {
  const m = firefoxManifest(base, 'me@example.com');
  assert.deepEqual(m.background, { scripts: ['background.js'], type: 'module' });
  assert.equal(m.minimum_chrome_version, undefined);
  assert.equal(m.browser_specific_settings.gecko.id, 'me@example.com');
  assert.deepEqual(m.browser_specific_settings.gecko.data_collection_permissions, { required: ['none'] });
  assert.ok(base.background.service_worker && base.minimum_chrome_version, 'the base manifest was not mutated');
});

test('both targets ask for exactly the same, minimal permissions and no host access', () => {
  const f = firefoxManifest(base);
  for (const m of [base, f]) {
    assert.deepEqual([...m.permissions].sort(), ['activeTab', 'contextMenus', 'storage']);
    assert.ok(!m.host_permissions && !m.optional_host_permissions && !m.content_scripts && !m.optional_permissions);
  }
  assert.equal(f.content_security_policy.extension_pages, base.content_security_policy.extension_pages);
});

test('only compat.js touches the chrome namespace, so the code runs wherever `browser` or `chrome` exists', () => {
  for (const f of fs.readdirSync(HERE).filter((n) => n.endsWith('.js') && n !== 'compat.js')) {
    assert.ok(!/\bchrome\./.test(fs.readFileSync(path.join(HERE, f), 'utf8')), `${f} uses chrome.* directly`);
  }
});

test('build writes both packages, each zip holds a valid manifest and every file the manifest and pages name', () => {
  const out = fs.mkdtempSync(path.join(os.tmpdir(), 'hl-ext-build-'));
  const made = build(out, 'build-test@example.com');
  for (const [name, { dir, zip }] of Object.entries(made)) {
    const listed = execFileSync('python3', ['-m', 'zipfile', '-l', zip]).toString();
    for (const needed of ['manifest.json', 'background.js', 'compat.js', 'popup.html', 'options.html', 'icons/128.png']) {
      assert.ok(listed.includes(needed), `${name} zip lacks ${needed}`);
    }
    const m = JSON.parse(fs.readFileSync(path.join(dir, 'manifest.json'), 'utf8'));
    for (const icon of Object.values(m.icons)) assert.ok(fs.existsSync(path.join(dir, icon)), `${name}: missing ${icon}`);
    for (const page of [m.action.default_popup, m.options_ui.page]) {
      for (const [, src] of fs.readFileSync(path.join(dir, page), 'utf8').matchAll(/(?:src|href)="([^"]+)"/g)) {
        assert.ok(fs.existsSync(path.join(dir, src)), `${name}/${page} references missing ${src}`);
      }
    }
  }
  assert.equal(JSON.parse(fs.readFileSync(path.join(made.firefox.dir, 'manifest.json'), 'utf8')).browser_specific_settings.gecko.id, 'build-test@example.com');
  assert.ok(DEFAULT_GECKO_ID.includes('@'));
});
