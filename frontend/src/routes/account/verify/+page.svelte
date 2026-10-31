<script>
  import { onMount } from 'svelte';
  import { ApiError, api } from '#lib/api.js';
  import { t } from '#lib/i18n.svelte.js';

  let error = $state('');
  let notice = $state('');
  let working = $state(true);

  onMount(async () => {
    const token = new URLSearchParams(location.hash.slice(1)).get('token') ?? '';
    history.replaceState(null, '', location.pathname); // the token stays out of the address bar and history
    try {
      // Confirmation is a POST sent by this page, not by loading the link: mail scanners that open links change nothing.
      await api('/api/v1/auth/verify', { method: 'POST', body: { token } });
      notice = t('auth.verify.done');
    } catch (e) {
      error = e instanceof ApiError ? e.message : e?.name === 'TypeError' ? t('error.network') : t('error.generic');
    } finally {
      working = false;
    }
  });
</script>

<h1>{t('auth.verify.title')}</h1>
{#if working}<p class="muted">{t('auth.verify.working')}</p>{/if}
<p class="err" role="alert">{error}</p>
<p class="ok" role="status">{notice}</p>
<p><a href="/account/">{t('auth.back')}</a></p>
