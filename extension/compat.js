// One name for the extension API. Firefox (and Safari) expose the promise-based `browser` namespace; Chromium exposes
// `chrome`, which also returns promises in Manifest V3. Every other file uses `ext`, never `chrome` directly.
export const ext = globalThis.browser ?? globalThis.chrome;
