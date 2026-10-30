<script>
  import { ApiError, api } from '#lib/api.js';
  import { getLocale, t } from '#lib/i18n.svelte.js';

  let { me } = $props();

  let keys = $state([]);
  let usage = $state(null);
  let name = $state('');
  let fresh = $state(null); // the only time the key and webhook secret are visible
  let copied = $state('');
  let error = $state('');

  const locked = $derived(me?.limits?.enforced && me?.tier !== 'pro');

  // (re)load when the plan changes, e.g. right after a successful payment unlocks keys
  $effect(() => {
    if (!locked) load();
  });

  async function load() {
    if (locked) return;
    keys = await api('/api/v1/me/keys').catch(() => []);
    const u = await api('/api/v1/me/usage?days=30').catch(() => null);
    usage = u ? u.account.reduce((n, d) => n + (d.jobs ?? 0), 0) : null;
  }

  async function create(event) {
    event.preventDefault();
    error = '';
    try {
      fresh = await api('/api/v1/me/keys', { method: 'POST', body: { name } });
      name = '';
      await load();
    } catch (e) {
      error = e instanceof ApiError ? e.message : t('error.generic');
    }
  }

  async function revoke(id) {
    await api(`/api/v1/me/keys/${id}`, { method: 'DELETE' }).catch(() => {});
    if (fresh?.id === id) fresh = null;
    await load();
  }

  async function copy(label, text) {
    try {
      await navigator.clipboard.writeText(text);
      copied = label;
      setTimeout(() => (copied = ''), 2000);
    } catch {
      /* clipboard can be blocked; the value stays selectable on screen */
    }
  }

  const when = (ts) => new Date(ts * 1000).toLocaleString(getLocale());
</script>

<section class="card" aria-labelledby="keys-h">
  <h2 id="keys-h">{t('keys.title')}</h2>
  <p class="muted">{t('keys.intro')}</p>

  {#if locked}
    <p>{t('keys.need_pro')}</p>
  {:else}
    {#if usage !== null}<p class="muted">{t('usage.title')}: {t('usage.jobs', { count: usage })}</p>{/if}
    <p class="err" role="alert">{error}</p>

    {#if fresh}
      <div class="reveal" role="status">
        <strong>{t('keys.new_title')}</strong>
        <p class="muted">{t('keys.new_note')}</p>
        {#each [['keys.key', fresh.key], ['keys.secret', fresh.webhook_secret]] as [label, value]}
          <label for={label}>{t(label)}</label>
          <div class="copyrow">
            <input id={label} type="text" readonly {value} onfocus={(e) => e.currentTarget.select()} />
            <button type="button" class="secondary" onclick={() => copy(label, value)}>{copied === label ? t('keys.copied') : t('keys.copy')}</button>
          </div>
        {/each}
      </div>
    {/if}

    <form onsubmit={create}>
      <div class="row">
        <div class="grow">
          <label for="keyname">{t('keys.name')}</label>
          <input id="keyname" type="text" maxlength="64" required bind:value={name} />
        </div>
        <button type="submit">{t('keys.create')}</button>
      </div>
    </form>

    {#if keys.length}
      <ul class="keys">
        {#each keys as k}
          <li>
            <div class="grow">
              <strong>{k.name}</strong> <code>hl_{k.id}_…</code>
              <div class="muted small">{k.last_used ? t('keys.last_used', { when: when(k.last_used) }) : t('keys.never')}</div>
            </div>
            <button class="secondary" onclick={() => revoke(k.id)}>{t('keys.revoke')}</button>
          </li>
        {/each}
      </ul>
    {:else}
      <p class="muted">{t('keys.none')}</p>
    {/if}
  {/if}
</section>

<style>
  .reveal { border: 2px solid var(--accent); border-radius: 8px; padding: 12px; margin: 12px 0; }
  .copyrow { display: flex; gap: 8px; margin-bottom: 8px; }
  .copyrow input { font-family: ui-monospace, monospace; font-size: 0.85rem; }
  .keys { list-style: none; padding: 0; margin: 12px 0 0; display: grid; grid-template-columns: minmax(0, 1fr); gap: 8px; }
  .keys .grow { flex: 1; min-width: 0; overflow-wrap: anywhere; }
  .keys button { flex: none; }
  .keys li { display: flex; gap: 8px; align-items: center; border-bottom: 1px solid var(--line); padding-bottom: 8px; }
  .small { font-size: 0.85rem; }
</style>
