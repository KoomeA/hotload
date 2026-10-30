<script>
  import { LOCALES, getLocale, setLocale, t } from '#lib/i18n.svelte.js';
  import { session } from '#lib/session.svelte.js';
  import { getTheme, setTheme } from '#lib/theme.svelte.js';
</script>

<header>
  <a class="logo" href="/" aria-label={t('home')}>Hot<span>load</span></a>
  <nav>
    <a href="/account/">{session.user ? session.user.email : t('account')}</a>
    <label class="sr-only" for="lang">{t('language')}</label>
    <select id="lang" value={getLocale()} onchange={(e) => setLocale(e.currentTarget.value)}>
      {#each Object.entries(LOCALES) as [code, l]}<option value={code}>{l.name}</option>{/each}
    </select>
    <label class="sr-only" for="theme">{t('theme')}</label>
    <select id="theme" value={getTheme()} onchange={(e) => setTheme(e.currentTarget.value)}>
      <option value="system">{t('theme.system')}</option>
      <option value="light">{t('theme.light')}</option>
      <option value="dark">{t('theme.dark')}</option>
    </select>
  </nav>
</header>

<style>
  header { max-width: 680px; margin: 0 auto; padding: 12px 16px 0; display: flex; gap: 12px; align-items: center; justify-content: space-between; flex-wrap: wrap; }
  .logo { font-size: 1.4rem; font-weight: 800; text-decoration: none; color: var(--fg); }
  .logo span { color: var(--accent); }
  nav { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  nav a { padding: 8px; max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  nav select { width: auto; min-height: 40px; padding: 6px 8px; }
</style>
