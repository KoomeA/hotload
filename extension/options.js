import { PRESETS, authHeaders, errorText, normalizeServer } from './lib.js';
import { ext } from './compat.js';

const $ = (id) => document.getElementById(id);
const say = (text, bad = false) => {
  $('msg').textContent = text;
  $('msg').className = bad ? 'err' : 'ok';
};

for (const [value, label] of PRESETS) $('preset').add(new Option(label, value));

ext.storage.local.get(['server', 'key', 'preset']).then(({ server, key, preset }) => {
  $('server').value = server ?? '';
  $('key').value = key ?? '';
  if (preset) $('preset').value = preset;
});

$('form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const server = normalizeServer($('server').value);
  if (!server) return say('Enter a valid https address (http only for localhost).', true);
  await ext.storage.local.set({ server, key: $('key').value.trim(), preset: $('preset').value });
  $('server').value = server;
  say('Saved.');
});

$('test').addEventListener('click', async () => {
  const server = normalizeServer($('server').value);
  if (!server) return say('Enter a valid server address first.', true);
  try {
    const status = await fetch(`${server}/api/v1/status`);
    if (!status.ok) return say(`Server answered ${status.status}.`, true);
    const key = $('key').value.trim();
    if (key) {
      const r = await fetch(`${server}/api/v1/me/usage?days=1`, { headers: authHeaders(key) });
      if (!r.ok) return say(errorText(r.status, await r.json().catch(() => null)), true);
      return say('Connected. Your API key works.');
    }
    say('Connected. Add an API key for one-click downloads.');
  } catch {
    say('Cannot reach that server.', true);
  }
});
