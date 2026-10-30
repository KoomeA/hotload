<script>
  // Internal console: English only on purpose. Access is enforced by the server (admin flag set from the CLI).
  import { onMount } from 'svelte';
  import { ApiError, api } from '#lib/api.js';
  import { session } from '#lib/session.svelte.js';

  let allowed = $state(null);
  let overview = $state(null);
  let reports = $state([]);
  let rules = $state([]);
  let error = $state('');
  let notice = $state('');
  let block = $state({ kind: 'host', value: '', reason: '' });
  let grant = $state({ email: '', tier: 'premium', days: 30 });
  let take = $state({}); // per-report takedown choice: {kind, value}

  onMount(load);

  const fail = (e) => (error = e instanceof ApiError ? e.message : 'Request failed');

  async function load() {
    try {
      overview = await api('/api/v1/admin/overview');
      allowed = true;
      const open = await api('/api/v1/admin/reports?status=open');
      rules = await api('/api/v1/admin/blocklist');
      // Build the per-report form state BEFORE the reports are rendered, or the template reads undefined.
      take = Object.fromEntries(open.map((r) => [r.id, { on: false, kind: 'host', value: hostOf(r.url) }]));
      reports = open;
    } catch (e) {
      allowed = false;
      if (!(e instanceof ApiError) || ![401, 403].includes(e.status)) fail(e);
    }
  }

  function hostOf(url) {
    try {
      return new URL(url).hostname;
    } catch {
      return url;
    }
  }

  async function resolve(r, status) {
    error = notice = '';
    const t = take[r.id];
    try {
      await api(`/api/v1/admin/reports/${r.id}/resolve`, {
        method: 'POST',
        body: { status, block: status === 'actioned' && t.on ? { kind: t.kind, value: t.value, reason: `report ${r.id}` } : null }
      });
      notice = `Report ${status}.`;
      await load();
    } catch (e) {
      fail(e);
    }
  }

  async function addRule(event) {
    event.preventDefault();
    error = notice = '';
    try {
      await api('/api/v1/admin/blocklist', { method: 'POST', body: block });
      block = { kind: 'host', value: '', reason: '' };
      await load();
    } catch (e) {
      fail(e);
    }
  }

  async function removeRule(id) {
    await api(`/api/v1/admin/blocklist/${id}`, { method: 'DELETE' }).catch(fail);
    await load();
  }

  async function grantPlan(event) {
    event.preventDefault();
    error = notice = '';
    try {
      const r = await api('/api/v1/admin/users/tier', { method: 'POST', body: { ...grant, days: grant.tier === 'free' ? null : Number(grant.days) } });
      notice = `${r.email} is now ${r.tier}${r.tier_expires ? ' until ' + new Date(r.tier_expires * 1000).toLocaleDateString() : ''}.`;
    } catch (e) {
      fail(e);
    }
  }
</script>

<h1>Admin</h1>
<p class="err" role="alert">{error}</p>
<p class="ok" role="status">{notice}</p>

{#if allowed === null}
  <p class="muted">…</p>
{:else if !allowed}
  <p>{session.user ? 'This account is not an administrator.' : 'Sign in with an administrator account.'}</p>
{:else}
  <section class="card">
    <h2>Overview</h2>
    <p>{overview.users} users · {overview.paid_payments} paid payments · {overview.blocked} block rules ·
      <strong>{overview.open_reports} open reports</strong></p>
  </section>

  <section class="card">
    <h2>Open reports</h2>
    {#if !reports.length}<p class="muted">Nothing to review.</p>{/if}
    {#each reports as r}
      <article class="report">
        <p><strong class:err={r.kind === 'csam'}>{r.kind}</strong> · {new Date(r.created * 1000).toLocaleString()} · <span class="muted">{r.contact}</span></p>
        <p class="url">{r.url}</p>
        {#if r.details}<p class="muted">{r.details}</p>{/if}
        {#if r.kind === 'csam'}<p class="err">Do not open this link. Follow your legal reporting procedure (see docs/COMPLIANCE.md).</p>{/if}
        <div class="row">
          <label class="inline"><input type="checkbox" bind:checked={take[r.id].on} /> Block</label>
          <select bind:value={take[r.id].kind} aria-label="Rule kind">
            <option value="host">host</option><option value="url">exact URL</option><option value="media">media id</option>
          </select>
          <input type="text" bind:value={take[r.id].value} aria-label="Rule value" placeholder={take[r.id].kind === 'media' ? 'Youtube:VIDEO_ID' : ''} />
        </div>
        <div class="row">
          <button onclick={() => resolve(r, 'actioned')}>Action</button>
          <button class="secondary" onclick={() => resolve(r, 'rejected')}>Reject</button>
        </div>
      </article>
    {/each}
  </section>

  <section class="card">
    <h2>Blocklist</h2>
    <form onsubmit={addRule}>
      <div class="row">
        <select bind:value={block.kind} aria-label="Rule kind"><option value="host">host</option><option value="url">exact URL</option><option value="media">media id</option></select>
        <input class="grow" type="text" required bind:value={block.value} aria-label="Rule value" placeholder={block.kind === 'media' ? 'Youtube:VIDEO_ID' : 'example.com'} />
        <input class="grow" type="text" bind:value={block.reason} aria-label="Reason" placeholder="Reason" maxlength="200" />
        <button type="submit">Add</button>
      </div>
    </form>
    <ul class="rules">
      {#each rules as r}
        <li><span><code>{r.kind}</code> {r.value} <span class="muted">{r.reason}</span></span>
          <button class="secondary" onclick={() => removeRule(r.id)}>Remove</button></li>
      {/each}
    </ul>
  </section>

  <section class="card">
    <h2>Grant a plan</h2>
    <form onsubmit={grantPlan}>
      <div class="row">
        <input class="grow" type="email" required bind:value={grant.email} aria-label="Account email" placeholder="user@example.com" />
        <select bind:value={grant.tier} aria-label="Plan"><option>free</option><option>premium</option><option>pro</option></select>
        {#if grant.tier !== 'free'}<input type="number" min="1" max="3660" bind:value={grant.days} aria-label="Days" style="max-width: 100px" />{/if}
        <button type="submit">Apply</button>
      </div>
    </form>
  </section>
{/if}

<style>
  .report { border-top: 1px solid var(--line); padding-top: 8px; margin-top: 12px; }
  .url { word-break: break-all; font-family: ui-monospace, monospace; font-size: 0.85rem; }
  .rules { list-style: none; padding: 0; margin: 12px 0 0; display: grid; grid-template-columns: minmax(0, 1fr); gap: 6px; }
  .rules li { display: flex; justify-content: space-between; gap: 8px; align-items: center; word-break: break-all; }
</style>
