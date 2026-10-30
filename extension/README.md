# Hotload browser extension

Manifest V3, no build step. Works in Chrome, Edge, Brave and other Chromium browsers.

- **Toolbar popup:** *Open in Hotload* sends the current page to your server's UI. With an API key it also offers
  one-click downloads with progress and a save link.
- **Right-click menu:** "Open in Hotload" on pages, links, videos and audio.
- **Permissions:** only `activeTab`, `contextMenus` and `storage`. It makes no requests until you configure a server,
  and it talks only to that server. No analytics. The API key is stored on the device (`storage.local`, not synced).

## Install (unpacked)
1. Open `chrome://extensions`, enable *Developer mode*, choose *Load unpacked*, select this `extension/` folder.
2. Open the extension's settings, enter your server (for example `https://hotload.example`), optionally an API key
   created in *Account → API keys* (Pro plan on the hosted service), and press *Test connection*.

## Package for a store
```bash
cd extension && python3 -m zipfile -c ../hotload-extension.zip manifest.json *.js *.html *.css icons
```
Store listings need a privacy policy; the statement above is accurate for this version. Store submission and review
are out of scope of this repository.

## Development
`node --test extension/lib.test.mjs` runs the unit tests for the pure helpers in `lib.js`.
The popup accepts `?url=…&title=…` so it can be opened as a normal tab for automated tests.
UI strings are English only for now.
