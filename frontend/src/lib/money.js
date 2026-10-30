/** Format minor units (cents) in the user's language, e.g. (999, 'USD', 'de') -> "9,99 $". */
export function formatMoney(amountMinor, currency, locale = 'en') {
  try {
    const digits = new Intl.NumberFormat(locale, { style: 'currency', currency }).resolvedOptions().maximumFractionDigits;
    return new Intl.NumberFormat(locale, { style: 'currency', currency }).format(amountMinor / 10 ** digits);
  } catch {
    return `${(amountMinor / 100).toFixed(2)} ${currency}`;
  }
}

const LOCAL = new Set(['localhost', '127.0.0.1', '[::1]']);

/** Only follow checkout URLs that are https links (plain http only for localhost, i.e. local development).
 *  Never javascript:, data:, file:, or an unencrypted payment page. */
export function safeRedirect(url) {
  try {
    const u = new URL(url);
    if (u.protocol === 'https:') return u.href;
    return u.protocol === 'http:' && LOCAL.has(u.hostname) ? u.href : null;
  } catch {
    return null;
  }
}

/** The payment id a provider sent the buyer back with (ours, or Paystack's reference/trxref which equal it). */
export function returnedPaymentId(params) {
  for (const key of ['payment', 'reference', 'trxref']) {
    const v = params.get(key);
    if (v && /^[\w-]{6,64}$/.test(v)) return v;
  }
  return null;
}
