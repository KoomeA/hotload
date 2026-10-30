<script>
  import { onDestroy, onMount } from 'svelte';
  import { ApiError, api } from '#lib/api.js';
  import { getLocale, t } from '#lib/i18n.svelte.js';
  import { formatMoney, returnedPaymentId, safeRedirect } from '#lib/money.js';

  let { me, onchange } = $props();

  let catalog = $state({ providers: [], plans: [] });
  let payments = $state([]);
  let error = $state('');
  let busy = $state('');
  let banner = $state(null); // {kind: 'processing'|'paid'|'failed'|'expired'|'pending', tier?}
  let timer = null;

  const tiers = $derived(['premium', 'pro'].map((tier) => ({ tier, plans: catalog.plans.filter((p) => p.tier === tier) })).filter((g) => g.plans.length));

  onMount(async () => {
    catalog = await api('/api/v1/billing/plans').catch(() => catalog);
    await loadPayments();
    const id = returnedPaymentId(new URLSearchParams(location.search));
    if (id) {
      window.history.replaceState(null, '', '/account/');
      watch(id);
    }
  });
  onDestroy(() => clearTimeout(timer));

  async function loadPayments() {
    payments = await api('/api/v1/billing/payments').catch(() => []);
  }

  /** Poll the server (which asks the provider) until the payment settles; webhooks may be slow or delayed. */
  function watch(id, attempt = 0) {
    banner = { kind: 'processing' };
    api(`/api/v1/billing/payments/${id}`)
      .then(async (p) => {
        if (p.status === 'paid') {
          await onchange();
          await loadPayments();
          banner = { kind: 'paid' };
        } else if (p.status === 'failed' || p.status === 'expired') {
          await loadPayments();
          banner = { kind: p.status };
        } else if (attempt < 40) {
          timer = setTimeout(() => watch(id, attempt + 1), 3000);
        } else {
          banner = { kind: 'pending' };
        }
      })
      .catch(() => (banner = null));
  }

  async function buy(plan, option) {
    error = '';
    busy = `${plan.plan}:${option.provider}:${option.currency}`;
    try {
      const { url } = await api('/api/v1/billing/checkout', {
        method: 'POST',
        body: { plan: plan.plan, provider: option.provider, currency: option.currency }
      });
      const target = safeRedirect(url);
      if (!target) throw new ApiError(502, t('error.generic'));
      location.assign(target);
    } catch (e) {
      error = e instanceof ApiError ? e.message : t('error.generic');
      busy = '';
    }
  }

  const when = (ts) => new Date(ts * 1000).toLocaleDateString(getLocale());
</script>

<section class="card" aria-labelledby="plans-h">
  <h2 id="plans-h">{t('plans.title')}</h2>
  {#if me?.tier !== 'free' && me?.tier_expires}
    <p class="ok">{t('plans.ends', { date: when(me.tier_expires) })}</p>
  {/if}

  {#if banner}
    <p role="status" class:ok={banner.kind === 'paid'} class:err={banner.kind === 'failed' || banner.kind === 'expired'}>
      {#if banner.kind === 'processing'}{t('plans.processing')}
      {:else if banner.kind === 'paid'}{t('plans.paid', { tier: me?.tier ?? '' })}
      {:else if banner.kind === 'pending'}{t('plans.pending')}
      {:else}{t(`plans.${banner.kind}`)}{/if}
    </p>
  {/if}
  <p class="err" role="alert">{error}</p>

  {#if !catalog.plans.length}
    <p class="muted">{t('plans.none')}</p>
  {:else}
    <p class="muted">{t('plans.intro')}</p>
    {#each tiers as group}
      <div class="tier">
        {#each group.plans as plan}
          <div class="plan">
            <strong>{t(`plan.${plan.plan}`)}</strong>
            <div class="buttons">
              {#each plan.options as option}
                <button class="secondary" disabled={!!busy} onclick={() => buy(plan, option)}>
                  {t('plans.pay_with', { price: formatMoney(option.amount_minor, option.currency, getLocale()), method: t(`method.${option.provider}`) })}
                </button>
              {/each}
            </div>
          </div>
        {/each}
      </div>
    {/each}
  {/if}

  {#if payments.length}
    <h3 style="margin-top: 16px">{t('payments.title')}</h3>
    <ul class="pay">
      {#each payments as p}
        <li>
          <span>{t(`plan.${p.plan}`)} · {formatMoney(p.amount_minor, p.currency, getLocale())}</span>
          <span class="muted">{t(`payments.status.${p.status}`)} · {when(p.created)}</span>
        </li>
      {/each}
    </ul>
  {/if}
</section>

<style>
  .tier { border-top: 1px solid var(--line); padding-top: 8px; margin-top: 8px; }
  .plan { margin: 8px 0 12px; }
  .buttons { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 6px; }
  .pay { list-style: none; padding: 0; margin: 8px 0 0; display: grid; grid-template-columns: minmax(0, 1fr); gap: 4px; }
  .pay li { display: flex; justify-content: space-between; gap: 8px; flex-wrap: wrap; font-size: 0.9rem; }
</style>
