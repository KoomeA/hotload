# Payments: research, decision and design

Research date: 2026-10-30. Figures come from secondary sources that disagree with each other; treat every number as an
estimate to confirm with a written quote. Paystack's and BTCPay's own documentation pages could not be fetched from the
research environment (HTTP 403 / empty), so API details below were cross-checked against third-party guides and must be
confirmed in Paystack **test mode** and a BTCPay **staging store** before launch.

## 1. The finding that shapes everything

**Paystack's Acceptable Use Policy bars obscene material and pornography, and anything that infringes copyright or other
proprietary rights.** Its "Restricted Business List" is jurisdiction-specific and is the controlling document
([Paystack terms](https://paystack.com/terms), as summarised by search; the list itself could not be retrieved).

Hotload supports adult sites (a launch requirement) and downloads from platforms whose terms forbid downloading. A
processor that bans both can freeze funds or terminate the account. Consequences for the design:

1. **Disclose the business accurately** in every processor application, including adult-site support. Misrepresenting
   it (for example choosing a misleading business category) is the usual reason merchants lose held funds, and it
   can create liability. Do not do it.
2. **Get Paystack's written position** before depending on it. If it declines, it cannot be a launch rail.
3. **Never run on a single rail.** The billing layer is provider-agnostic; one rail failing must not stop revenue.
4. **Sweep settlements regularly** and keep operating cash out of any processor balance.

## 2. Options compared

### Cards and local methods
| Option | Strengths | Concerns (from sources) |
|---|---|---|
| **Paystack** (Kenya: cards, M-Pesa; also NG/GH/ZA) | Local methods incl. M-Pesa; simple API; HMAC-signed webhooks | AUP bars pornography and copyright-infringing items. **M-Pesa has no recurring billing** (Paystack support: "subscriptions can be done through card payments"). Amounts are in the smallest unit (KES cents). |
| **CCBill / Segpay / Verotel / Epoch** (adult-friendly card processors) | Built for adult and high-risk traffic; recurring billing | Reported fees ~10-15% on CCBill and Verotel; Segpay/Epoch quoted lower by some sources; reserves 5-10% for 90-180 days (Epoch reportedly none); annual scheme-registration fees reported at about $1,000-1,950; exit holds of ~6 months; chargeback ratio above ~1% triggers fees or termination. They do not publish rates: get quotes. |

Sources: [Payments Edge comparison](https://thepaymentsedge.com/payments/processor-showdown-2025/),
[exclu.at](https://exclu.at/blog/adult-payment-processor),
[adults.dev](https://www.adults.dev/Article/adult-payment-processing-ccbill-vs-verotel-vs-epoch-vs-stripe-full-comparison),
[paymentreview.com](https://paymentreview.com/comparisons/ccbill-vs-verotel/).

### Crypto
| Option | Fees | KYC / custody | Notes |
|---|---|---|---|
| **BTCPay Server** (self-hosted) | 0% processing (you pay hosting + network fees) | None; non-custodial | No content rules from the software, but your **hosting provider's AUP** still applies. You operate it. |
| **NOWPayments** | ~0.5-1% (sources differ) | Custodial gateway; KYC possible if flagged | Adult policy not found: ask. |
| **CoinGate** | ~1% | KYC for merchants reported | Adult policy not found: ask. |
| **Coinbase Commerce** | ~1% | KYC | One source reports it closed to merchants outside US/Singapore on 2026-03-31 (others still list it): verify. Not recommended. |

Sources: [Aurpay](https://aurpay.net/aurspace/crypto-payment-gateway-comparison-2026/),
[Spark](https://www.spark.money/tools/crypto-payment-gateway-comparison),
[Wootoshi](https://wootoshi.com/blog/btcpay-vs-coinbase-commerce.html).
None of the sources addressed adult-content policies: confirm each provider's rules before signing.

## 3. Recommendation

1. **Product model: prepaid passes, not auto-renewing subscriptions.** A pass buys 30 or 365 days of a plan.
   It works on every rail (M-Pesa and crypto cannot recur), avoids the surprise-renewal chargebacks that threaten
   processor standing, and makes accounting simple. Card auto-renew can be added later on rails that support it.
2. **Rails, in priority order**
   - **BTCPay Server** (crypto, non-custodial, no counterparty who can freeze funds). Implemented.
   - **Paystack** (cards and M-Pesa for Kenyan/African customers) **only after written approval**. Implemented; off by default.
   - **An adult-friendly card processor** (CCBill, Segpay, Verotel, or Epoch): request quotes now, in parallel. The
     provider interface (`src/hotload/billing/base.py`) is ready for it; each needs about one module plus tests.
3. **Chargeback hygiene (applies to any card rail):** clear billing descriptor, instant email receipts, explicit price
   and duration before payment, a plain refund policy, no free-trial-to-paid tricks, fast support replies.
4. **Application pack for processors:** the age gate and policy in `docs/COMPLIANCE.md`, Terms of Service, DMCA/abuse
   process (the public report form in M3), privacy policy, a statement that Hotload hosts no media long-term.

## 4. How it is built

- Catalog (`billing/catalog.py`): plans `premium_30`, `premium_365`, `pro_30`, `pro_365`, priced in USD by default
  (matching the research targets: $9.99/$83.99 and $29.99/$239.99). **Prices for other currencies (KES, NGN...) are
  not guessed**: set `HOTLOAD_PRICES` with your own amounts in minor units.
- A `Payment` row is created before redirecting to the provider; its id is the reference sent to the provider.
- **Webhooks are never trusted on their own.** Signature is checked on the raw body, then the payment is re-verified
  server-side with the provider (amount and currency must match what we expected) before any entitlement is granted.
  Granting is idempotent: only a `pending -> paid` transition grants, so retried webhooks cannot double-extend a plan.
- Entitlement: `users.tier` + `users.tier_expires`; expired plans fall back to `free` automatically. Buying the same
  plan extends from the current expiry; buying a higher plan switches immediately (no proration); buying a lower plan
  while a higher one is active is refused.
- Paystack signature: `x-paystack-signature` = HMAC-SHA512 of the raw body keyed with the secret key. BTCPay:
  `BTCPay-Sig: sha256=<HMAC-SHA256 of the body keyed with the webhook secret>`; the webhook payload carries the invoice
  id, so the invoice is fetched from BTCPay to learn its status, amount and our order id.

## 5. Refunds, disputes and chargebacks

Payments move through `pending -> paid`, and after that `paid -> refunded`, `paid -> disputed -> paid | chargedback`,
`paid -> chargedback`, and `held -> refunded` (a held payment never granted anything). Every change is one database
transaction with a compare-and-set on the status, so retried webhooks and double clicks change nothing twice, and each is
written to an append-only audit trail (`payment_events`: who, what, when, note, and what happened to the plan).

**Entitlement follows the money**, with the arithmetic the grant used: refunding or charging back a payment takes its days
off the plan's expiry (two stacked purchases, one refunded: one purchase's days remain); nothing left means free. A dispute
takes the days back immediately and winning it grants the plan again from that moment. Known edges, all deliberate:
- The user has since moved to a *higher* plan: their access is left alone and the result says `skipped_higher_plan`
  (the higher plan's days cannot be attributed to the cheaper payment). Decide by hand whether to adjust it.
- A manual perpetual grant or an already lapsed plan has no days of this payment left to take.
- A won dispute for a user who now has a higher plan, or no account, is `held` for an operator, not granted.
- A refunded, disputed or charged-back payment is never granted again, even if the provider keeps saying "success".

**Who does what**
- Admin console `/admin/` -> "Payments needing a decision" lists `held` and `disputed` payments. Refund (optionally *through
  Paystack*), "Dispute won: restore plan", "Dispute lost" (chargeback). API: `GET /api/v1/admin/payments`,
  `GET .../payments/{id}` (with its audit trail), `POST .../payments/{id}/refund | dispute | dispute-won | chargeback`.
- Provider webhooks (Paystack): a signed `refund.processed` is recorded as a refund; a signed `charge.dispute.create` suspends
  the plan. A signed `charge.dispute.resolve` is only *logged at ERROR* and added to the audit trail: how a dispute ended is
  not acted on automatically, because processors word outcomes differently. The overview counts `disputed_payments`.
- BTCPay (crypto) has no chargebacks and no refund API here: refund on-chain or from the BTCPay dashboard, then record it in
  the console without "through provider". Any other processor's dispute notice (email, dashboard) is recorded the same way.

**Not verified against the live provider** (the same caveat as the rest of this file): Paystack's `POST /refund` request and
the event names/field locations for `refund.processed`, `charge.dispute.create` and `charge.dispute.resolve` were written from
memory of its documentation, which could not be fetched. The code treats an unrecognised event as ignored, so a wrong field name
fails safe (nothing happens automatically; the console still works). Confirm in Paystack test mode, then correct
`billing/paystack.py` if needed.

**Not built:** partial refunds, automatic account suspension after repeated chargebacks (a person who charges back can open a
new account: watch the `payment CHARGEBACK` ERROR logs), refund and receipt emails to the customer, and CCBill/Segpay-style
processor modules (their webhook formats are not known here).

## 6. Before taking real money

- [ ] Written answers from Paystack (and any crypto gateway) about adult-site support and a download service.
- [ ] Quotes from at least two adult-friendly card processors (rate, reserve %/duration, registration fee, exit hold).
- [ ] Paystack test-mode run through `/api/v1/billing/checkout` and the webhook (mocked in tests, never run live here), including a refund and a dispute event (section 5).
- [ ] BTCPay staging run: create invoice, pay on testnet/regtest, confirm `InvoiceSettled` grants the plan.
- [ ] Legal review of Terms, refund policy and the DMCA agent registration; tax treatment (VAT/digital services).
- [ ] Rotate keys into a secret manager; set `HOTLOAD_SECRET_KEY`, HTTPS, and `HOTLOAD_PUBLIC_URL`.
