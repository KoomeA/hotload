<script>
  import { ApiError, api } from '#lib/api.js';
  import { t } from '#lib/i18n.svelte.js';

  let url = $state('');
  let kind = $state('copyright');
  let contact = $state('');
  let details = $state('');
  let busy = $state(false);
  let error = $state('');
  let doneId = $state('');

  async function send(event) {
    event.preventDefault();
    error = '';
    busy = true;
    try {
      const r = await api('/api/v1/reports', { method: 'POST', body: { url, kind, contact, details } });
      doneId = r.id;
    } catch (e) {
      error = e instanceof ApiError ? e.message : t('error.generic');
    } finally {
      busy = false;
    }
  }
</script>

<h1>{t('report.title')}</h1>
<p class="muted">{t('report.intro')}</p>

{#if doneId}
  <p class="ok" role="status">{t('report.thanks', { id: doneId })}</p>
{:else}
  <form class="card" onsubmit={send}>
    <p class="err" role="alert">{error}</p>
    <label for="r-url">{t('report.url')}</label>
    <input id="r-url" type="url" required maxlength="2048" placeholder="https://…" bind:value={url} />
    <div class="row">
      <div class="grow">
        <label for="r-kind">{t('report.kind')}</label>
        <select id="r-kind" bind:value={kind}>
          {#each ['copyright', 'abuse', 'csam', 'other'] as k}<option value={k}>{t(`report.kind.${k}`)}</option>{/each}
        </select>
      </div>
      <div class="grow">
        <label for="r-contact">{t('report.contact')}</label>
        <input id="r-contact" type="email" required autocomplete="email" bind:value={contact} />
      </div>
    </div>
    <div class="row">
      <div class="grow">
        <label for="r-details">{t('report.details')}</label>
        <textarea id="r-details" rows="4" maxlength="2000" bind:value={details}></textarea>
      </div>
    </div>
    <div class="row"><button type="submit" disabled={busy}>{t('report.send')}</button></div>
  </form>
{/if}
