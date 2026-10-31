# Hotload browser extension

Manifest V3. One codebase for Chromium browsers (Chrome, Edge, Brave) and Firefox; only the manifest differs, and
`build.mjs` writes both.

- **Toolbar popup:** *Open in Hotload* sends the current page to your server's UI. With an API key it also offers
  one-click downloads with progress and a save link.
- **Right-click menu:** "Open in Hotload" on pages, links, videos and audio.
- **Permissions:** only `activeTab`, `contextMenus` and `storage`. It makes no requests until you configure a server,
  and it talks only to that server. No analytics. The API key is stored on the device (`storage.local`, not synced).

## Install (unpacked)
**Chromium:** open `chrome://extensions`, enable *Developer mode*, *Load unpacked*, select this `extension/` folder.

**Firefox** (temporary, until restart): `node extension/build.mjs`, then open `about:debugging#/runtime/this-firefox`,
*Load Temporary Add-on...*, and pick `dist/extension/firefox/manifest.json`. A permanent install needs the signed package
(see below).

Then open the extension's settings, enter your server (for example `https://hotload.example`), optionally an API key
created in *Account -> API keys* (Pro plan on the hosted service), and press *Test connection*.

## Package for a store
```bash
node extension/build.mjs        # -> dist/extension/{chrome,firefox}/ and hotload-chrome.zip, hotload-firefox.zip
```
- **Firefox needs an add-on id** that is yours and unique (AMO signing, and storage in Firefox is keyed by it). The build
  uses a placeholder, `hotload-extension@example.org`; set your own first: `HOTLOAD_EXT_ID=hotload@your.domain node extension/build.mjs`.
  Changing the id later makes Firefox treat it as a different add-on, so choose once.
- What differs in the Firefox manifest: an event page (`background.scripts`) instead of a service worker (Firefox has no
  extension service workers), `browser_specific_settings.gecko` (id, minimum version 115, and the "no data collected"
  declaration AMO asks for), and no `minimum_chrome_version`. Permissions are identical: `activeTab`, `contextMenus`, `storage`.
- Safari is not built: it needs Xcode (`xcrun safari-web-extension-converter`) and an Apple developer account.
- Store listings need a privacy policy; the statement above is accurate for this version. Store submission and review
  are out of scope of this repository.

## Development
`node --test extension/lib.test.mjs extension/build.test.mjs` runs the unit tests (helpers in `lib.js`; the manifests and
packages from `build.mjs`). Only `compat.js` touches the `chrome` namespace: every other file uses `ext`, which is
`browser` where it exists (Firefox) and `chrome` otherwise.

Browser tests against a running stack: `node extension/e2e.mjs` (Chromium, Playwright) and
`node extension/e2e-firefox.mjs` (real Firefox over Marionette, with its own throwaway stack; needs `FIREFOX=/path/to/firefox`,
`HL_BIN=/path/to/venv/bin`, `redis-server`).
The popup accepts `?url=…&title=…` so it can be opened as a normal tab for automated tests.
UI strings are English only for now.
