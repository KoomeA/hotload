import de from './locales/de.js';
import en from './locales/en.js';
import es from './locales/es.js';
import fr from './locales/fr.js';
import pt from './locales/pt.js';
import sw from './locales/sw.js';

export const LOCALES = {
  en: { name: 'English', messages: en },
  es: { name: 'Español', messages: es },
  fr: { name: 'Français', messages: fr },
  de: { name: 'Deutsch', messages: de },
  pt: { name: 'Português', messages: pt },
  sw: { name: 'Kiswahili', messages: sw }
};

/** Choose the best supported locale for a browser language list like ["pt-BR", "en"]. */
export function pickLocale(languages, available = Object.keys(LOCALES)) {
  for (const lang of languages ?? []) {
    const base = String(lang).toLowerCase().split('-')[0];
    if (available.includes(base)) return base;
  }
  return 'en';
}

/** Look up a message, falling back to English then to the key; replaces {name} placeholders. */
export function translate(locale, key, vars = {}) {
  const raw = LOCALES[locale]?.messages[key] ?? en[key] ?? key;
  return raw.replace(/\{(\w+)\}/g, (_, name) => (name in vars ? String(vars[name]) : `{${name}}`));
}

let current = $state('en');

export function t(key, vars) {
  return translate(current, key, vars);
}

export function getLocale() {
  return current;
}

export function setLocale(locale) {
  if (!(locale in LOCALES)) return;
  current = locale;
  try {
    localStorage.setItem('hl-locale', locale);
    document.documentElement.lang = locale;
  } catch {
    /* storage can be unavailable (private mode); the choice then lasts for this visit only */
  }
}

export function initLocale() {
  let saved = null;
  try {
    saved = localStorage.getItem('hl-locale');
  } catch {
    /* ignore */
  }
  setLocale(saved && saved in LOCALES ? saved : pickLocale(navigator.languages));
}
