<script>
  import '../app.css';
  import { onMount } from 'svelte';
  import Header from '#lib/components/Header.svelte';
  import { initLocale, t } from '#lib/i18n.svelte.js';
  import { refreshSession } from '#lib/session.svelte.js';
  import { initTheme } from '#lib/theme.svelte.js';

  let { children } = $props();

  onMount(() => {
    initLocale();
    initTheme();
    refreshSession();
    if ('serviceWorker' in navigator) navigator.serviceWorker.register('/service-worker.js').catch(() => {});
  });
</script>

<a class="sr-only" href="#main">{t('skip')}</a>
<Header />
<main id="main">
  {@render children()}
  <p class="muted" style="margin-top: 32px; font-size: 0.85rem">{t('footer')} · <a href="/report/">{t('report.link')}</a></p>
</main>
