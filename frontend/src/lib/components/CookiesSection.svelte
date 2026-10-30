<script>
  import { onMount } from 'svelte';
  import { ApiError, api } from '#lib/api.js';
  import { t } from '#lib/i18n.svelte.js';

  let { onchange = () => {} } = $props();

  let rows = $state([]);
  let status = $state('loading'); // loading | ready | off | plan
  let site = $state('');
  let text = $state('');
  let error = $state('');
  let notice = $state('');

  onMount(load);

  async function load() {
    try {
      rows = await api('/api/v1/me/cookies');
      status = 'ready';
    } catch (e) {
      status = e instanceof ApiError && e.status === 503 ? 'off' : e instanceof ApiError && e.status === 403 ? 'plan' : 'ready';
    }
    onchange(rows);
  }

  async function save(event) {
    event.preventDefault();
    error = notice = '';
    try {
      const r = await api('/api/v1/me/cookies', { method: 'PUT', body: { domain: site, cookies_txt: text } });
      notice = t('cookies.saved', { n: r.cookies, site: r.domain });
      site = text = ''; // the pasted cookies must not linger in the page
      await load();
    } catch (e) {
      error = e instanceof ApiError ? e.message : t('error.generic');
    }
  }

  async function remove(domain) {
    await api(`/api/v1/me/cookies/${encodeURIComponent(domain)}`, { method: 'DELETE' }).catch(() => {});
    await load();
  }
</script>

<section class="card" aria-labelledby="cookies-h">
  <h2 id="cookies-h">{t('cookies.title')}</h2>
  <p class="muted">{t('cookies.intro')}</p>
  {#if status === 'off'}
    <p>{t('cookies.off')}</p>
  {:else if status === 'plan'}
    <p>{t('cookies.plan')}</p>
  {:else if status === 'ready'}
    <p class="err" role="alert">{error}</p>
    <p class="ok" role="status">{notice}</p>
    <form onsubmit={save}>
      <label for="ck-site">{t('cookies.site')}</label>
      <input id="ck-site" type="text" required autocomplete="off" spellcheck="false" bind:value={site} />
      <label for="ck-text" style="margin-top: 8px">{t('cookies.paste')}</label>
      <textarea id="ck-text" rows="4" required autocomplete="off" spellcheck="false" bind:value={text}></textarea>
      <div class="row"><button type="submit">{t('cookies.save')}</button></div>
    </form>
    {#if rows.length}
      <ul class="ck">
        {#each rows as r}
          <li>
            <span class="grow"><strong>{r.domain}</strong> <span class="muted">{t('cookies.count', { n: r.cookies })}</span></span>
            <button class="secondary" onclick={() => remove(r.domain)}>{t('cookies.delete')}</button>
          </li>
        {/each}
      </ul>
    {:else}
      <p class="muted">{t('cookies.none')}</p>
    {/if}
  {/if}
</section>

<style>
  .ck { list-style: none; padding: 0; margin: 12px 0 0; display: grid; grid-template-columns: minmax(0, 1fr); gap: 8px; }
  .ck li { display: flex; gap: 8px; align-items: center; border-bottom: 1px solid var(--line); padding-bottom: 8px; }
  .ck .grow { flex: 1; min-width: 0; overflow-wrap: anywhere; }
  .ck button { flex: none; }
</style>
