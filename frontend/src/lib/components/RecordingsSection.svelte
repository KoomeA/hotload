<script>
  import { onDestroy } from 'svelte';
  import { ApiError, api } from '#lib/api.js';
  import { t } from '#lib/i18n.svelte.js';

  let { me } = $props();

  const FORMATS = ['best_mp4', '1080p', '720p', '480p', 'mp3'];
  let rows = $state([]);
  let url = $state('');
  let startLocal = $state('');
  let minutes = $state(30);
  let format = $state('best_mp4');
  let adult = $state(false);
  let error = $state('');
  let notice = $state('');
  let busy = $state(false);
  let timer = null;

  const max = $derived(me?.limits?.live_minutes ?? 60);
  const allowed = $derived((me?.limits?.scheduled ?? 0) > 0);
  const active = $derived(rows.some((r) => r.state === 'waiting' || (r.state === 'queued' && r.job && !['done', 'error'].includes(r.job.state))));

  onDestroy(() => clearTimeout(timer));

  $effect(() => {
    if (me?.limits?.scheduled > 0) load();
  });

  async function load() {
    clearTimeout(timer);
    rows = await api('/api/v1/schedules').catch(() => rows);
    if (active) timer = setTimeout(load, 15000);
  }

  const when = (ts) => new Date(ts * 1000).toLocaleString();
  const host = (u) => {
    try {
      return new URL(u).hostname + new URL(u).pathname.slice(0, 30);
    } catch {
      return u;
    }
  };

  async function create(event) {
    event.preventDefault();
    error = notice = '';
    busy = true;
    try {
      const body = { url, minutes: Number(minutes), preset: format, age_confirmed: adult };
      if (startLocal) body.start_at = new Date(startLocal).toISOString();
      await api('/api/v1/schedules', { method: 'POST', body });
      url = startLocal = '';
      notice = t('rec.created');
      await load();
    } catch (e) {
      error = e instanceof ApiError ? e.message : e?.name === 'TypeError' ? t('error.network') : t('error.generic');
    } finally {
      busy = false;
    }
  }

  async function remove(id) {
    error = notice = '';
    try {
      await api(`/api/v1/schedules/${id}`, { method: 'DELETE' });
    } catch (e) {
      error = e instanceof ApiError ? e.message : t('error.generic');
    }
    await load();
  }
</script>

<section class="card" id="recordings">
  <h2>{t('rec.title')}</h2>
  {#if !allowed}
    <p class="muted">{t('rec.upgrade')}</p>
  {:else}
    <p class="muted">{t('rec.intro')}</p>
    <p class="err" role="alert">{error}</p>
    <p class="ok" role="status">{notice}</p>
    <form onsubmit={create}>
      <label for="rec-url">{t('rec.link')}</label>
      <input id="rec-url" type="url" required maxlength="2048" bind:value={url} />
      <div class="row">
        <div class="grow">
          <label for="rec-start">{t('rec.start')}</label>
          <input id="rec-start" type="datetime-local" bind:value={startLocal} aria-describedby="rec-start-hint" />
        </div>
        <div>
          <label for="rec-min">{t('rec.minutes')}</label>
          <input id="rec-min" type="number" min="1" max={max} required bind:value={minutes} style="max-width: 100px" />
        </div>
        <div>
          <label for="rec-format">{t('rec.format')}</label>
          <select id="rec-format" bind:value={format}>
            {#each FORMATS as f}<option value={f}>{t(`preset.${f}`)}</option>{/each}
          </select>
        </div>
      </div>
      <p id="rec-start-hint" class="muted" style="margin: 4px 0 0">{t('rec.start_hint')}</p>
      <label class="inline"><input type="checkbox" bind:checked={adult} /> {t('rec.adult')}</label>
      <div class="row"><button type="submit" disabled={busy}>{t('rec.schedule')}</button></div>
    </form>

    {#if rows.length}
      <ul class="recs">
        {#each rows as r (r.id)}
          <li>
            <div class="grow">
              <div class="title">{host(r.url)}</div>
              <div class="muted small">{t(`rec.state.${r.state}`)} · {when(r.start_at)} · {t('rec.length', { minutes: r.minutes })}</div>
              {#if r.job && r.job.state !== 'done'}
                <div class="muted small">{r.job.message}{r.job.state === 'running' ? ` (${Math.round(r.job.progress)}%)` : ''}</div>
              {:else if r.state !== 'queued' && r.message && r.state !== 'cancelled'}
                <div class="muted small">{r.message}</div>
              {/if}
            </div>
            {#if r.job?.download_url}<a class="btn" href={r.job.download_url}>{t('rec.download')}</a>{/if}
            {#if r.state === 'waiting'}
              <button class="secondary" onclick={() => remove(r.id)}>{t('rec.cancel')}</button>
            {:else if r.state !== 'queued' || r.job?.state === 'done' || r.job?.state === 'error'}
              <button class="secondary" onclick={() => remove(r.id)}>{t('rec.remove')}</button>
            {/if}
          </li>
        {/each}
      </ul>
    {:else}
      <p class="muted">{t('rec.empty')}</p>
    {/if}
  {/if}
</section>

<style>
  .recs { list-style: none; padding: 0; margin: 12px 0 0; display: grid; grid-template-columns: minmax(0, 1fr); gap: 8px; }
  .recs li { display: flex; gap: 8px; align-items: center; border-bottom: 1px solid var(--line); padding-bottom: 8px; }
  .recs .grow { flex: 1; min-width: 0; }
  .recs .btn, .recs button { flex: none; }
  .title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .small { font-size: 0.85rem; }
</style>
