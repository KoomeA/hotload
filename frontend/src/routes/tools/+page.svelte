<script>
  import { onMount } from 'svelte';
  import { t } from '#lib/i18n.svelte.js';

  let origin = $state('');
  onMount(() => (origin = location.origin));

  // Opens the current page in Hotload. Built from this server's own origin; the page address is URL-encoded.
  const bookmarklet = $derived(origin ? `javascript:(()=>{location.href='${origin}/?url='+encodeURIComponent(location.href)})()` : '#');
</script>

<h1>{t('tools.title')}</h1>

<section class="card">
  <h2>{t('tools.ext.title')}</h2>
  <p>{t('tools.ext.text')}</p>
  <label for="srv">{t('tools.server')}</label>
  <input id="srv" type="text" readonly value={origin} onfocus={(e) => e.currentTarget.select()} />
</section>

<section class="card">
  <h2>{t('tools.bookmarklet.title')}</h2>
  <p>{t('tools.bookmarklet.text')}</p>
  <!-- Drag target only: clicking a javascript: link on this page is blocked by the page's own security policy. -->
  <a class="btn" id="bookmarklet" href={bookmarklet} draggable="true">{t('tools.bookmarklet.button')}</a>
</section>

<section class="card">
  <h2>{t('tools.share.title')}</h2>
  <p>{t('tools.share.text')}</p>
</section>

<section class="card">
  <h2>{t('tools.api.title')}</h2>
  <p>{t('tools.api.text')}</p>
  <a class="btn secondary" href="/account/">{t('account')}</a>
</section>
