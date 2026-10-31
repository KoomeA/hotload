<script>
  import { onMount } from 'svelte';
  import { ApiError, api } from '#lib/api.js';
  import { t } from '#lib/i18n.svelte.js';

  let mode = $state('request'); // request: ask for a link; choose: a link arrived; done
  let token = '';
  let email = $state('');
  let password = $state('');
  let error = $state('');
  let notice = $state('');
  let busy = $state(false);
  let emailOn = $state(true);

  function readToken() {
    // The token travels in the URL fragment so it never reaches a server, proxy log or Referer header; take it out of
    // the address bar as soon as it has been read.
    const found = new URLSearchParams(location.hash.slice(1)).get('token') ?? '';
    if (!found) return;
    token = found;
    mode = 'choose';
    error = notice = '';
    history.replaceState(null, '', location.pathname);
  }

  onMount(() => {
    readToken();
    // A link opened while this page is already showing only changes the fragment.
    addEventListener('hashchange', readToken);
    api('/api/v1/auth/providers').then((p) => (emailOn = p.email)).catch(() => {});
    return () => removeEventListener('hashchange', readToken);
  });

  const fail = (e) => (error = e instanceof ApiError ? e.message : e?.name === 'TypeError' ? t('error.network') : t('error.generic'));

  async function request(event) {
    event.preventDefault();
    error = notice = '';
    busy = true;
    try {
      await api('/api/v1/auth/password/forgot', { method: 'POST', body: { email } });
      notice = t('auth.forgot.sent');
    } catch (e) {
      fail(e);
    } finally {
      busy = false;
    }
  }

  async function choose(event) {
    event.preventDefault();
    error = notice = '';
    busy = true;
    try {
      await api('/api/v1/auth/password/reset', { method: 'POST', body: { token, password } });
      password = '';
      mode = 'done';
      notice = t('auth.reset.done');
    } catch (e) {
      fail(e);
    } finally {
      busy = false;
    }
  }
</script>

<h1>{mode === 'choose' ? t('auth.reset.title') : t('auth.forgot.title')}</h1>
<p class="err" role="alert">{error}</p>
<p class="ok" role="status">{notice}</p>

{#if mode === 'request'}
  <section class="card">
    {#if emailOn}
      <form onsubmit={request}>
        <label for="email">{t('email')}</label>
        <input id="email" type="email" autocomplete="email" required bind:value={email} />
        <div class="row"><button type="submit" disabled={busy}>{t('auth.forgot.send')}</button></div>
      </form>
    {/if}
  </section>
{:else if mode === 'choose'}
  <section class="card">
    <form onsubmit={choose}>
      <label for="password">{t('password')}</label>
      <input id="password" type="password" required minlength="10" maxlength="128" autocomplete="new-password" bind:value={password}
        aria-describedby="pw-hint" />
      <p id="pw-hint" class="muted" style="margin: 4px 0 0">{t('password.hint')}</p>
      <div class="row"><button type="submit" disabled={busy}>{t('auth.reset.save')}</button></div>
    </form>
  </section>
{/if}
<p><a href="/account/">{t('auth.back')}</a></p>
