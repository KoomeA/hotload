// Builds the store packages: dist/chrome/ + hotload-chrome.zip and dist/firefox/ + hotload-firefox.zip.
//   node extension/build.mjs            (HOTLOAD_EXT_ID=you@yourdomain sets the Firefox add-on id)
// The sources are one codebase; only the manifest differs. Chromium wants a service worker, Firefox an event page.
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FILES = ['background.js', 'compat.js', 'lib.js', 'options.html', 'options.js', 'popup.html', 'popup.js', 'styles.css'];

// An add-on id is required by Firefox (storage, signing). It must be yours and unique: set HOTLOAD_EXT_ID before publishing.
export const DEFAULT_GECKO_ID = 'hotload-extension@example.org';

export function firefoxManifest(base, id = DEFAULT_GECKO_ID) {
  const m = structuredClone(base);
  delete m.minimum_chrome_version;
  m.background = { scripts: ['background.js'], type: 'module' }; // Firefox has no extension service workers
  m.browser_specific_settings = {
    gecko: { id, strict_min_version: '115.0', data_collection_permissions: { required: ['none'] } }
  };
  return m;
}

export function build(outRoot = path.join(HERE, '..', 'dist', 'extension'), id = process.env.HOTLOAD_EXT_ID || DEFAULT_GECKO_ID) {
  const base = JSON.parse(fs.readFileSync(path.join(HERE, 'manifest.json'), 'utf8'));
  const targets = { chrome: base, firefox: firefoxManifest(base, id) };
  const made = {};
  for (const [name, manifest] of Object.entries(targets)) {
    const dir = path.join(outRoot, name);
    fs.rmSync(dir, { recursive: true, force: true });
    fs.mkdirSync(path.join(dir, 'icons'), { recursive: true });
    for (const f of FILES) fs.copyFileSync(path.join(HERE, f), path.join(dir, f));
    for (const f of fs.readdirSync(path.join(HERE, 'icons'))) fs.copyFileSync(path.join(HERE, 'icons', f), path.join(dir, 'icons', f));
    fs.writeFileSync(path.join(dir, 'manifest.json'), JSON.stringify(manifest, null, 2) + '\n');
    const zip = path.join(outRoot, `hotload-${name}.zip`);
    fs.rmSync(zip, { force: true });
    execFileSync('python3', ['-m', 'zipfile', '-c', zip, ...fs.readdirSync(dir)], { cwd: dir });
    made[name] = { dir, zip };
  }
  return made;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  for (const [name, { dir, zip }] of Object.entries(build())) console.log(`${name}: ${path.relative(process.cwd(), dir)}  ${path.relative(process.cwd(), zip)}`);
}
