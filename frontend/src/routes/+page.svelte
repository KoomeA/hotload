<script>
  import { onDestroy, onMount } from 'svelte';
  import JobStatus from '#lib/components/JobStatus.svelte';
  import TrimScrubber from '#lib/components/TrimScrubber.svelte';
  import { ApiError, api, watchJob } from '#lib/api.js';
  import { getLocale, t } from '#lib/i18n.svelte.js';
  import { isVideoInPlaylist, looksLikePlaylist, sharedUrl, urlsFromText } from '#lib/share.js';
  import { session } from '#lib/session.svelte.js';
  import { formatTime } from '#lib/time.js';

  let input = $state('');
  let busy = $state(false);
  let error = $state('');
  let degraded = $state([]);
  let presets = $state([]);

  // what we're showing: one video ("single"), a playlist, or several pasted links ("batch")
  let mode = $state('idle');
  let info = $state(null);
  let playlist = $state(null);
  let picked = $state({}); // playlist url -> bool
  let urls = $state([]);

  // options
  let preset = $state('best_mp4');
  let audioLang = $state('');
  let subLangs = $state([]);
  let subAuto = $state(false);
  let subFormat = $state('srt');
  let subMode = $state('file');
  let trimOn = $state(false);
  let start = $state(0);
  let end = $state(0);
  let trimValid = $state(true);
  let sponsorblock = $state('off');
  let liveOn = $state(false);
  let liveMinutes = $state(10);
  let useCookies = $state(false);
  let savedSites = $state([]); // domains the signed-in user has saved cookies for
  let ageOk = $state(false);
  let ageNeeded = $state(false);

  // progress
  let job = $state(null);
  let batch = $state(null);
  let stopWatching = null;
  let poll = null;

  const groups = $derived({
    video: presets.filter((p) => !p.audio && p.key !== 'subtitles'),
    audio: presets.filter((p) => p.audio),
    text: presets.filter((p) => p.key === 'subtitles')
  });
  const isAudio = $derived(presets.find((p) => p.key === preset)?.audio ?? false);
  const isSubsOnly = $derived(preset === 'subtitles');
  const subOptions = $derived(info ? [...new Set([...(info.subtitles ?? []), ...(subAuto ? (info.auto_captions ?? []) : [])])] : []);
  const finalUrls = $derived(
    mode === 'playlist' ? playlist.entries.filter((e) => picked[e.url]).map((e) => e.url) : mode === 'batch' ? urls : []
  );
  const firstUrl = $derived(urlsFromText(input)[0] ?? '');
  const hostOfInput = $derived((() => { try { return new URL(firstUrl).hostname; } catch { return ''; } })());
  const cookieMatch = $derived(!!session.user && !!hostOfInput && savedSites.some((d) => hostOfInput === d || hostOfInput.endsWith('.' + d)));
  const isYoutube = $derived(info?.site === 'Youtube');
  const working = $derived(!!job && (job.state === 'queued' || job.state === 'running'));

  onMount(async () => {
    const shared = sharedUrl(new URLSearchParams(location.search));
    if (shared) {
      input = shared;
      history.replaceState(null, '', '/'); // keep shared links out of the address bar and history
      submit();
    }
    api('/api/v1/presets').then((p) => (presets = p)).catch(() => {});
    api('/api/v1/me/cookies').then((rows) => (savedSites = rows.map((r) => r.domain))).catch(() => {});
    api('/api/v1/status')
      .then((s) => (degraded = s.canary?.degraded ?? []))
      .catch(() => {});
  });
  onDestroy(() => {
    stopWatching?.();
    clearInterval(poll);
  });

  function langName(code) {
    try {
      return new Intl.DisplayNames([getLocale()], { type: 'language' }).of(code) || code;
    } catch {
      return code;
    }
  }

  function fail(e) {
    error = e instanceof ApiError ? e.message : e?.name === 'TypeError' ? t('error.network') : t('error.generic');
    if (e instanceof ApiError && e.status === 451) ageNeeded = true;
  }

  function reset() {
    stopWatching?.();
    clearInterval(poll);
    error = '';
    info = playlist = job = batch = null;
    mode = 'idle';
    ageOk = ageNeeded = false;
    audioLang = '';
    subLangs = [];
    trimOn = false;
    sponsorblock = 'off';
    liveOn = false;
  }

  async function submit(event) {
    event?.preventDefault();
    reset();
    const found = urlsFromText(input);
    if (!found.length) return fail(new ApiError(400, t('url.label')));
    busy = true;
    try {
      if (found.length > 1) {
        urls = found;
        mode = 'batch';
      } else if (looksLikePlaylist(found[0])) {
        await loadPlaylist(found[0]);
      } else {
        info = await api('/api/v1/extract', { method: 'POST', body: { url: found[0], use_cookies: cookieMatch && useCookies } });
        mode = 'single';
        ageNeeded = info.age_gate_required;
        end = Math.floor(info.duration ?? 0);
        start = 0;
        liveOn = !!info.is_live && info.live_status !== 'is_upcoming';
      }
    } catch (e) {
      fail(e);
    } finally {
      busy = false;
    }
  }

  async function loadPlaylist(url) {
    const data = await api('/api/v1/playlist', { method: 'POST', body: { url } });
    if (!data.playlist) {
      info = await api('/api/v1/extract', { method: 'POST', body: { url, use_cookies: cookieMatch && useCookies } });
      mode = 'single';
      ageNeeded = info.age_gate_required;
      end = Math.floor(info.duration ?? 0);
      return;
    }
    playlist = data;
    picked = Object.fromEntries(data.entries.map((e) => [e.url, true]));
    mode = 'playlist';
  }

  async function wholePlaylist() {
    const url = urlsFromText(input)[0];
    busy = true;
    error = '';
    try {
      const listId = new URL(url).searchParams.get('list');
      const base = `${new URL(url).origin}/playlist?list=${encodeURIComponent(listId)}`;
      info = null;
      await loadPlaylist(base);
    } catch (e) {
      fail(e);
    } finally {
      busy = false;
    }
  }

  function options() {
    const o = { preset, age_confirmed: ageOk };
    if (audioLang) o.audio_lang = audioLang;
    if (subLangs.length) {
      Object.assign(o, { subtitle_langs: subLangs, subtitle_format: subFormat, subtitle_auto: subAuto,
        subtitle_mode: isAudio || isSubsOnly ? 'file' : subMode });
    }
    if (mode === 'single' && trimOn && !isSubsOnly) Object.assign(o, { start, end });
    if (mode === 'single' && isYoutube && sponsorblock !== 'off' && !trimOn && !isSubsOnly && !liveOn) o.sponsorblock = sponsorblock;
    if (mode === 'single' && liveOn && !isSubsOnly) o.live_minutes = Number(liveMinutes);
    if (cookieMatch && useCookies) o.use_cookies = true;
    return o;
  }

  async function startDownload() {
    error = '';
    if (ageNeeded && !ageOk) return (error = t('age.required'));
    if (isSubsOnly && !subLangs.length) return (error = t('subs.pick_one'));
    if (trimOn && !trimValid) return (error = t('trim.invalid'));
    if (mode !== 'single' && !finalUrls.length) return (error = t('playlist.none'));
    busy = true;
    stopWatching?.();
    clearInterval(poll);
    try {
      if (mode === 'single') {
        job = await api('/api/v1/jobs', { method: 'POST', body: { url: urlsFromText(input)[0], ...options() } });
        stopWatching = watchJob(job.id, (j) => (job = j), () => (error = t('error.network')));
      } else {
        batch = await api('/api/v1/batches', { method: 'POST', body: { urls: finalUrls, ...options() } });
        poll = setInterval(async () => {
          try {
            batch = await api(`/api/v1/batches/${batch.id}`);
            if (batch.finished) clearInterval(poll);
          } catch {
            clearInterval(poll);
          }
        }, 1500);
      }
    } catch (e) {
      fail(e);
    } finally {
      busy = false;
    }
  }

  const toggleLang = (code) => (subLangs = subLangs.includes(code) ? subLangs.filter((c) => c !== code) : [...subLangs, code]);
</script>

<h1>{t('app.tagline')}</h1>

{#if degraded.length}
  <p class="muted" role="status">{t('status.degraded', { sites: degraded.join(', ') })}</p>
{/if}

<form class="card" onsubmit={submit}>
  <label for="url">{t('url.label')}</label>
  <textarea id="url" rows="2" inputmode="url" autocomplete="off" autocapitalize="off" spellcheck="false"
    placeholder={t('url.placeholder')} bind:value={input}></textarea>
  {#if cookieMatch}
    <div class="row"><label class="inline"><input type="checkbox" bind:checked={useCookies} /> {t('cookies.use')}</label></div>
  {/if}
  <div class="row"><button type="submit" disabled={busy || !input.trim()}>{busy && mode === 'idle' ? t('fetching') : t('fetch')}</button></div>
</form>

<p class="err" role="alert">{error}</p>

{#if mode !== 'idle'}
  <section class="card" aria-label={t('options')}>
    {#if mode === 'single' && info}
      <div class="media">
        {#if info.thumbnail}<img src={info.thumbnail} alt="" referrerpolicy="no-referrer" loading="lazy" />{/if}
        <div>
          <h2>{info.title ?? '—'}</h2>
          <p class="muted">{[info.site, info.uploader, info.duration ? formatTime(info.duration) : null].filter(Boolean).join(' · ')}</p>
        </div>
      </div>
      {#if isVideoInPlaylist(input)}
        <div class="row"><button class="secondary" type="button" onclick={wholePlaylist} disabled={busy}>{t('playlist.load')}</button></div>
      {/if}
    {:else if mode === 'playlist'}
      <h2>{t('playlist.title', { title: playlist.title ?? '' })}</h2>
      <p class="muted">{t('playlist.count', { count: playlist.count })}</p>
      <label class="inline"><input type="checkbox" checked={finalUrls.length === playlist.entries.length}
        onchange={(e) => (picked = Object.fromEntries(playlist.entries.map((x) => [x.url, e.currentTarget.checked])))} />
        {t('playlist.select_all')}</label>
      <ul class="entries">
        {#each playlist.entries as e}
          <li><label class="inline"><input type="checkbox" bind:checked={picked[e.url]} />
            <span>{e.title ?? e.url}{#if e.duration} <span class="muted">({formatTime(e.duration)})</span>{/if}</span></label></li>
        {/each}
      </ul>
    {:else}
      <h2>{t('batch.title', { total: urls.length })}</h2>
      <ul class="entries">{#each urls as u}<li class="muted">{u}</li>{/each}</ul>
    {/if}

    <div class="row">
      <div class="grow">
        <label for="preset">{t('format')}</label>
        <select id="preset" bind:value={preset}>
          {#each Object.entries(groups) as [g, list]}
            {#if list.length}
              <optgroup label={t(`group.${g}`)}>
                {#each list as p}<option value={p.key}>{t(`preset.${p.key}`)}</option>{/each}
              </optgroup>
            {/if}
          {/each}
        </select>
      </div>
      {#if mode === 'single' && info?.audio_langs?.length > 1}
        <div class="grow">
          <label for="audio">{t('audio.track')}</label>
          <select id="audio" bind:value={audioLang}>
            <option value="">{t('audio.default')}</option>
            {#each info.audio_langs as code}<option value={code}>{langName(code)}</option>{/each}
          </select>
        </div>
      {/if}
    </div>

    {#if mode === 'single' && info}
      <fieldset>
        <legend>{t('subs.title')}</legend>
        {#if subOptions.length || info.auto_captions?.length}
          {#if info.auto_captions?.length}
            <label class="inline"><input type="checkbox" bind:checked={subAuto} /> {t('subs.auto')}</label>
          {/if}
          <div class="chips" style="margin-top: 8px">
            {#each subOptions as code}
              <label class="inline"><input type="checkbox" checked={subLangs.includes(code)} onchange={() => toggleLang(code)} />{langName(code)}</label>
            {/each}
          </div>
          {#if subLangs.length}
            <div class="row">
              <div class="grow">
                <label for="subfmt">{t('subs.format')}</label>
                <select id="subfmt" bind:value={subFormat}><option value="srt">SRT</option><option value="vtt">VTT</option></select>
              </div>
              {#if !isAudio && !isSubsOnly}
                <div class="grow">
                  <label for="submode">{t('subs.mode')}</label>
                  <select id="submode" bind:value={subMode}>
                    <option value="file">{t('subs.mode_file')}</option><option value="embed">{t('subs.mode_embed')}</option>
                  </select>
                </div>
              {/if}
            </div>
          {/if}
        {:else}
          <p class="muted">{t('subs.none')}</p>
        {/if}
      </fieldset>

      {#if info.duration && !isSubsOnly && !info.is_live}
        <fieldset>
          <legend>{t('trim.title')}</legend>
          <label class="inline"><input type="checkbox" bind:checked={trimOn} /> {t('trim.enable')}</label>
          {#if trimOn}<TrimScrubber duration={Math.floor(info.duration)} bind:start bind:end bind:valid={trimValid} />{/if}
        </fieldset>
      {/if}
    {/if}

    {#if mode === 'single' && info}
      {#if info.is_live || info.live_status === 'is_upcoming'}
        <fieldset>
          <legend>{t('live.title')}</legend>
          {#if info.live_status === 'is_upcoming'}
            <p class="muted">{t('live.upcoming')}</p>
          {:else}
            <p>{t('live.is_live')}</p>
            <label class="inline"><input type="checkbox" bind:checked={liveOn} /> {t('live.record')}</label>
            {#if liveOn}
              <div class="row">
                <div class="grow">
                  <label for="live-min">{t('live.minutes')}</label>
                  <input id="live-min" type="number" min="1" max="240" bind:value={liveMinutes} />
                </div>
              </div>
            {/if}
            <p class="muted" style="margin-top: 8px">{t('live.note')}</p>
          {/if}
        </fieldset>
      {/if}

      {#if isYoutube && !isSubsOnly && !liveOn}
        <fieldset>
          <legend>{t('sb.title')}</legend>
          <label for="sb" class="sr-only">{t('sb.title')}</label>
          <select id="sb" bind:value={sponsorblock} disabled={trimOn} aria-describedby={trimOn ? 'sb-note' : undefined}>
            <option value="off">{t('sb.off')}</option>
            <option value="mark">{t('sb.mark')}</option>
            <option value="remove">{t('sb.remove')}</option>
          </select>
          {#if trimOn}<p id="sb-note" class="muted">{t('sb.no_trim')}</p>{/if}
        </fieldset>
      {/if}
    {/if}

    {#if ageNeeded}
      <div class="row"><label class="inline"><input type="checkbox" bind:checked={ageOk} /> {t('age.confirm')}</label></div>
    {/if}

    <div class="row">
      <button type="button" onclick={startDownload} disabled={busy || working}>
        {mode === 'playlist' ? t('playlist.download', { count: finalUrls.length }) : t('download')}
      </button>
    </div>

    {#if job}<JobStatus {job} />{/if}

    {#if batch}
      <div class="job-list" aria-live="polite">
        <p>{t('batch.progress', { done: batch.done, total: batch.total })}</p>
        <progress max={batch.total} value={batch.done + batch.jobs.filter((j) => j.state === 'error').length}></progress>
        {#if batch.zip_url}<a class="btn" href={batch.zip_url} download>{t('download_zip')}</a>{/if}
        {#each batch.jobs as j}<JobStatus job={j} label={j.title ?? ''} />{/each}
      </div>
    {/if}
  </section>
{/if}

<style>
  .media { display: flex; gap: 12px; align-items: flex-start; }
  .media img { width: 128px; height: 72px; object-fit: cover; border-radius: 8px; background: var(--line); flex: none; }
  .media h2 { word-break: break-word; }
  fieldset { border: 1px solid var(--line); border-radius: 8px; margin: 16px 0 0; padding: 12px; }
  legend { padding: 0 6px; font-weight: 600; }
  .entries { list-style: none; padding: 0; margin: 8px 0 0; max-height: 260px; overflow: auto; display: grid; grid-template-columns: minmax(0, 1fr); gap: 4px; }
  .entries li { word-break: break-all; }
  .job-list { margin-top: 16px; display: grid; gap: 4px; }
</style>
