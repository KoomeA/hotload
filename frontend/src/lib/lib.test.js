import { describe, expect, it } from 'vitest';
import { messageFrom } from './api.js';
import { LOCALES, pickLocale, translate } from './i18n.svelte.js';
import { isVideoInPlaylist, looksLikePlaylist, sharedUrl, urlsFromText } from './share.js';
import { clampRange, formatTime, parseTime } from './time.js';

describe('time', () => {
  it('parses seconds, mm:ss and h:mm:ss', () => {
    expect(parseTime('90')).toBe(90);
    expect(parseTime('1:30')).toBe(90);
    expect(parseTime('1:02:03')).toBe(3723);
    expect(parseTime(' 0:05.5 ')).toBe(5.5);
  });
  it('rejects junk and out-of-range minutes/seconds', () => {
    for (const bad of ['', 'abc', '1:75', '1:2:3:4', '-5', '1:', ':30', null, undefined]) expect(parseTime(bad)).toBeNull();
  });
  it('formats', () => {
    expect(formatTime(65)).toBe('1:05');
    expect(formatTime(3725)).toBe('1:02:05');
    expect(formatTime(-3)).toBe('0:00');
    expect(formatTime(parseTime('1:02:03'))).toBe('1:02:03');
  });
  it('clamps ranges keeping a minimum gap inside the duration', () => {
    expect(clampRange(10, 5, 100)).toEqual([10, 11]);
    expect(clampRange(-5, 500, 100)).toEqual([0, 100]);
    expect(clampRange(99.5, 100, 100)).toEqual([99, 100]);
  });
});

describe('share', () => {
  it('finds links inside shared text and trims punctuation', () => {
    expect(urlsFromText('Watch this (https://example.com/v?x=1), wow! and https://a.org/b.')).toEqual([
      'https://example.com/v?x=1',
      'https://a.org/b'
    ]);
    expect(urlsFromText('no link here')).toEqual([]);
  });
  it('prefers the url param, then text, then title', () => {
    expect(sharedUrl(new URLSearchParams({ url: 'https://u.example/1', text: 'https://t.example/2' }))).toBe('https://u.example/1');
    expect(sharedUrl(new URLSearchParams({ text: 'look https://t.example/2 now' }))).toBe('https://t.example/2');
    expect(sharedUrl(new URLSearchParams({ title: 'https://x.example/3' }))).toBe('https://x.example/3');
    expect(sharedUrl(new URLSearchParams({}))).toBeNull();
  });
  it('ignores non-http schemes', () => {
    expect(urlsFromText('javascript:alert(1) ftp://x.org file:///etc/passwd')).toEqual([]);
  });
  it('spots playlists', () => {
    expect(looksLikePlaylist('https://www.youtube.com/playlist?list=PL1')).toBe(true);
    expect(looksLikePlaylist('https://www.youtube.com/watch?v=a&list=PL1')).toBe(false);
    expect(isVideoInPlaylist('https://www.youtube.com/watch?v=a&list=PL1')).toBe(true);
    expect(isVideoInPlaylist('https://www.youtube.com/playlist?list=PL1')).toBe(false);
    expect(looksLikePlaylist('https://www.youtube.com/watch?v=a')).toBe(false);
  });
});

describe('i18n', () => {
  const en = LOCALES.en.messages;
  const placeholders = (s) => [...s.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();

  it('every locale has exactly the English keys and placeholders', () => {
    for (const [code, { messages }] of Object.entries(LOCALES)) {
      expect(Object.keys(messages).sort(), code).toEqual(Object.keys(en).sort());
      for (const key of Object.keys(en)) expect(placeholders(messages[key]), `${code}:${key}`).toEqual(placeholders(en[key]));
    }
  });
  it('picks the first supported browser language, else English', () => {
    expect(pickLocale(['pt-BR', 'en'])).toBe('pt');
    expect(pickLocale(['ja', 'sw-KE'])).toBe('sw');
    expect(pickLocale(['ja', 'ko'])).toBe('en');
    expect(pickLocale(undefined)).toBe('en');
  });
  it('interpolates, falls back and tolerates missing variables', () => {
    expect(translate('de', 'batch.progress', { done: 2, total: 5 })).toBe('2 von 5 fertig');
    expect(translate('xx', 'download')).toBe('Download');
    expect(translate('es', 'no.such.key')).toBe('no.such.key');
    expect(translate('en', 'save')).toBe('Save {name}');
  });
});

describe('api', () => {
  it('extracts readable messages from FastAPI errors', () => {
    expect(messageFrom({ detail: 'Nope' }, 400)).toBe('Nope');
    expect(messageFrom({ detail: [{ msg: 'Value error, end must be greater than start' }] }, 422)).toBe('end must be greater than start');
    expect(messageFrom({}, 500)).toBe('Error 500');
  });
});

import { formatMoney, returnedPaymentId, safeRedirect } from './money.js';

describe('money', () => {
  it('formats minor units for the locale and survives bad currencies', () => {
    expect(formatMoney(999, 'USD', 'en')).toBe('$9.99');
    expect(formatMoney(129900, 'KES', 'en')).toMatch(/1,299/);
    expect(formatMoney(500, 'JPY', 'en')).toMatch(/500/);
    expect(formatMoney(1000, 'NOPE!', 'en')).toBe('10.00 NOPE!');
  });
  it('only follows http(s) checkout links', () => {
    expect(safeRedirect('https://checkout.paystack.com/abc')).toBe('https://checkout.paystack.com/abc');
    for (const bad of ['javascript:alert(1)', 'data:text/html,x', 'file:///etc/passwd', 'nonsense', '']) expect(safeRedirect(bad)).toBeNull();
  });
  it('reads our payment id, falling back to the provider reference', () => {
    expect(returnedPaymentId(new URLSearchParams('payment=AbC_123-xyz'))).toBe('AbC_123-xyz');
    expect(returnedPaymentId(new URLSearchParams('trxref=Pay_REF_99&reference=Pay_REF_99'))).toBe('Pay_REF_99');
    expect(returnedPaymentId(new URLSearchParams('payment=<script>'))).toBeNull();
    expect(returnedPaymentId(new URLSearchParams('payment=ab'))).toBeNull();
    expect(returnedPaymentId(new URLSearchParams(''))).toBeNull();
  });
});
