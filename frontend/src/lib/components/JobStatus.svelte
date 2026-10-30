<script>
  import { t } from '#lib/i18n.svelte.js';

  let { job, label = null } = $props();
  const text = $derived(job.state === 'error' ? job.message || t('status.error') : job.state === 'running' && job.message?.startsWith('Retrying') ? job.message : t(`status.${job.state}`));
</script>

<div class="job" aria-live="polite">
  {#if label}<div class="muted label">{label}</div>{/if}
  {#if job.state === 'queued' || job.state === 'running'}
    <progress max="100" value={job.progress} aria-label={text}></progress>
  {/if}
  <div class:err={job.state === 'error'} class:ok={job.state === 'done'}>{text}</div>
  {#if job.state === 'done' && job.download_url}
    <a class="btn" href={job.download_url} download>{t('save', { name: job.filename ?? '' })}</a>
  {/if}
</div>

<style>
  .job { display: grid; gap: 6px; margin-top: 12px; }
  .label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .btn { justify-self: start; word-break: break-all; }
</style>
