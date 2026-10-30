<script>
  import { onMount } from 'svelte';
  import CookiesSection from '#lib/components/CookiesSection.svelte';
  import KeysSection from '#lib/components/KeysSection.svelte';
  import PlansSection from '#lib/components/PlansSection.svelte';
  import { ApiError, api } from '#lib/api.js';
  import { t } from '#lib/i18n.svelte.js';
  import { refreshSession, session } from '#lib/session.svelte.js';

  let tab = $state('in');
  let email = $state('');
  let password = $state('');
  let error = $state('');
  let notice = $state('');
  let busy = $state(false);
  let providers = $state({ password: true, oauth: [] });
  let history = $state([]);
  let me = $state(null);
  let deletePassword = $state('');

  const oauthErrors = { oauth: 'oauth.error', unverified_email: 'oauth.unverified_email', closed: 'oauth.closed' };

  onMount(async () => {
    const code = new URLSearchParams(location.search).get('error');
    if (code) {
      error = t(oauthErrors[code] ?? 'error.generic');
      history_replace();
    }
    api('/api/v1/auth/providers').then((p) => (providers = p)).catch(() => {});
    await load();
  });

  const history_replace = () => window.history.replaceState(null, '', '/account/');

  async function load() {
    await refreshSession();
    if (session.user) {
      me = await api('/api/v1/me').catch(() => null);
      history = await api('/api/v1/me/history').catch(() => []);
    } else {
      me = null;
      history = [];
    }
  }

  function fail(e) {
    error = e instanceof ApiError ? e.message : e?.name === 'TypeError' ? t('error.network') : t('error.generic');
  }

  async function submit(event) {
    event.preventDefault();
    error = notice = '';
    busy = true;
    try {
      await api(`/api/v1/auth/${tab === 'in' ? 'login' : 'register'}`, { method: 'POST', body: { email, password } });
      password = '';
      await load();
    } catch (e) {
      fail(e);
    } finally {
      busy = false;
    }
  }

  async function signOut() {
    await api('/api/v1/auth/logout', { method: 'POST' }).catch(() => {});
    await load();
  }

  async function clearHistory() {
    await api('/api/v1/me/history', { method: 'DELETE' }).catch(fail);
    history = [];
    notice = t('history.cleared');
  }

  async function deleteAccount(event) {
    event.preventDefault();
    error = '';
    try {
      await api('/api/v1/me', { method: 'DELETE', body: { password: deletePassword || null } });
      deletePassword = '';
      await load();
    } catch (e) {
      fail(e);
    }
  }
</script>

<h1>{t('account')}</h1>
<p class="err" role="alert">{error}</p>
<p class="ok" role="status">{notice}</p>

{#if !session.loaded}
  <p class="muted">…</p>
{:else if !session.user}
  <section class="card">
    <div class="row" role="tablist" style="margin-top: 0">
      <button role="tab" aria-selected={tab === 'in'} class:secondary={tab !== 'in'} onclick={() => (tab = 'in')}>{t('sign_in')}</button>
      {#if providers.password}
        <button role="tab" aria-selected={tab === 'up'} class:secondary={tab !== 'up'} onclick={() => (tab = 'up')}>{t('sign_up')}</button>
      {/if}
    </div>
    <form onsubmit={submit}>
      <div class="row">
        <div class="grow">
          <label for="email">{t('email')}</label>
          <input id="email" type="email" autocomplete="email" required bind:value={email} />
        </div>
      </div>
      <div class="row">
        <div class="grow">
          <label for="password">{t('password')}</label>
          <input id="password" type="password" required minlength={tab === 'up' ? 10 : 1} maxlength="128"
            autocomplete={tab === 'up' ? 'new-password' : 'current-password'} bind:value={password} aria-describedby="pw-hint" />
          {#if tab === 'up'}<p id="pw-hint" class="muted" style="margin: 4px 0 0">{t('password.hint')}</p>{/if}
        </div>
      </div>
      <div class="row"><button type="submit" disabled={busy}>{tab === 'in' ? t('sign_in') : t('sign_up')}</button></div>
    </form>
    {#if providers.oauth.length}
      <p class="muted" style="margin-top: 16px">{t('oauth.or')}</p>
      <div class="row" style="margin-top: 0">
        {#each providers.oauth as p}
          <a class="btn secondary" href={`/api/v1/auth/oauth/${p}/login`}>{p === 'github' ? 'GitHub' : 'Google'}</a>
        {/each}
      </div>
    {/if}
  </section>
{:else}
  <section class="card">
    <p><strong>{session.user.email}</strong></p>
    {#if me}
      <h2>{t('plan.title')}</h2>
      <p>{t('plan.name', { tier: me.tier })}</p>
      {#if me.limits.enforced}
        <p>{t('plan.usage', { used: me.used_12h, limit: me.limits.jobs_per_12h })}</p>
        <progress max={me.limits.jobs_per_12h} value={me.used_12h}></progress>
        <p class="muted">{t('plan.max', { minutes: me.limits.max_minutes })}</p>
      {:else}
        <p class="muted">{t('plan.unlimited')}</p>
      {/if}
    {/if}
    <div class="row"><button class="secondary" onclick={signOut}>{t('sign_out')}</button></div>
  </section>

  <PlansSection {me} onchange={load} />

  {#if me}<KeysSection {me} />{/if}

  <CookiesSection />

  <section class="card">
    <h2>{t('history.title')}</h2>
    {#if history.length}
      <ul class="hist">
        {#each history as h}
          <li>
            <div class="grow">
              <div class="title">{h.title ?? h.url}</div>
              <div class="muted small">{[h.site, h.preset, new Date(h.created * 1000).toLocaleString()].filter(Boolean).join(' · ')}</div>
            </div>
            <a class="btn secondary" href={`/?url=${encodeURIComponent(h.url)}`}>{t('history.again')}</a>
          </li>
        {/each}
      </ul>
      <div class="row"><button class="secondary" onclick={clearHistory}>{t('history.clear')}</button></div>
    {:else}
      <p class="muted">{t('history.empty')}</p>
    {/if}
  </section>

  <section class="card">
    <h2>{t('danger.title')}</h2>
    <p class="muted">{t('danger.explain')}</p>
    <form onsubmit={deleteAccount}>
      {#if me?.has_password}
        <label for="delpw">{t('danger.confirm_password')}</label>
        <input id="delpw" type="password" autocomplete="current-password" required bind:value={deletePassword} />
      {/if}
      <div class="row"><button class="danger" type="submit">{t('danger.delete')}</button></div>
    </form>
  </section>
{/if}

<style>
  /* minmax(0, 1fr): a bare grid column will not shrink below its content, so a long URL would overflow the card */
  .hist { list-style: none; padding: 0; margin: 0; display: grid; grid-template-columns: minmax(0, 1fr); gap: 8px; }
  .hist li { display: flex; gap: 8px; align-items: center; border-bottom: 1px solid var(--line); padding-bottom: 8px; }
  /* min-width: 0 lets the text column shrink (and ellipsise) instead of pushing the button out of the card */
  .hist .grow { flex: 1; min-width: 0; }
  .hist .btn { flex: none; }
  .title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .small { font-size: 0.85rem; }
  [role='tab'][aria-selected='true'] { outline: 2px solid var(--accent); outline-offset: 2px; }
</style>
