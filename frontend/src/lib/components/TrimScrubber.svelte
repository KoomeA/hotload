<script>
  import { t } from '#lib/i18n.svelte.js';
  import { clampRange, formatTime, parseTime } from '#lib/time.js';

  /** duration in seconds; start/end are bound so the parent can send them to the API */
  let { duration, start = $bindable(0), end = $bindable(duration), valid = $bindable(true) } = $props();

  let startText = $state(formatTime(start));
  let endText = $state(formatTime(end));
  let invalid = $state(false);

  const pct = (v) => (duration ? (v / duration) * 100 : 0);
  // When the handles overlap near the right end, the start handle must be on top or it could never move left.
  const startOnTop = $derived(end - start < duration * 0.03 && start > duration / 2);

  function fromSliders(which, value) {
    const [s, e] = which === 'start' ? clampRange(value, end, duration) : clampRange(start, value, duration);
    start = s;
    end = e;
    startText = formatTime(s);
    endText = formatTime(e);
    invalid = false;
    valid = true;
  }

  function fromText() {
    const s = parseTime(startText);
    const e = parseTime(endText);
    invalid = s === null || e === null || e <= s || e > duration || s < 0;
    valid = !invalid;
    if (!invalid) {
      start = s;
      end = e;
    }
  }
</script>

<div class="scrub">
  <div class="track" style="--a: {pct(start)}%; --b: {pct(end)}%">
    <input type="range" min="0" max={duration} step="1" value={start} style:z-index={startOnTop ? 2 : 1} aria-label={t('trim.start_handle')}
      aria-valuetext={formatTime(start)} oninput={(e) => fromSliders('start', +e.currentTarget.value)} />
    <input type="range" min="0" max={duration} step="1" value={end} style:z-index={startOnTop ? 1 : 2} aria-label={t('trim.end_handle')}
      aria-valuetext={formatTime(end)} oninput={(e) => fromSliders('end', +e.currentTarget.value)} />
  </div>
  <div class="row">
    <div class="grow">
      <label for="trim-start">{t('trim.start')}</label>
      <input id="trim-start" type="text" inputmode="numeric" autocomplete="off" bind:value={startText}
        onchange={fromText} aria-invalid={invalid} aria-describedby="trim-msg" />
    </div>
    <div class="grow">
      <label for="trim-end">{t('trim.end')}</label>
      <input id="trim-end" type="text" inputmode="numeric" autocomplete="off" bind:value={endText}
        onchange={fromText} aria-invalid={invalid} aria-describedby="trim-msg" />
    </div>
  </div>
  <p id="trim-msg" class:err={invalid} class="muted" role="status" aria-live="polite">
    {invalid ? t('trim.invalid') : t('trim.length', { length: formatTime(end - start) })}
  </p>
</div>

<style>
  .track { position: relative; height: 44px; margin: 8px 4px 0;
    background: linear-gradient(to right, var(--line) 0 var(--a), var(--accent) var(--a) var(--b), var(--line) var(--b) 100%) center / 100% 8px no-repeat; }
  /* two stacked native range inputs: transparent tracks, only the thumbs are interactive */
  .track input[type='range'] { position: absolute; inset: 0; width: 100%; margin: 0; min-height: 44px; padding: 0;
    background: none; border: 0; pointer-events: none; appearance: none; -webkit-appearance: none; }
  .track input[type='range']::-webkit-slider-runnable-track { background: transparent; }
  .track input[type='range']::-moz-range-track { background: transparent; }
  .track input[type='range']::-webkit-slider-thumb { pointer-events: auto; appearance: none; -webkit-appearance: none;
    width: 24px; height: 24px; border-radius: 50%; background: var(--fg); border: 3px solid var(--bg); cursor: grab; }
  .track input[type='range']::-moz-range-thumb { pointer-events: auto; width: 18px; height: 18px; border-radius: 50%;
    background: var(--fg); border: 3px solid var(--bg); cursor: grab; }
</style>
