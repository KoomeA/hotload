let mode = $state('system'); // 'system' | 'light' | 'dark'

export function getTheme() {
  return mode;
}

function apply() {
  if (mode === 'system') document.documentElement.removeAttribute('data-theme');
  else document.documentElement.setAttribute('data-theme', mode);
}

export function setTheme(next) {
  mode = next;
  try {
    localStorage.setItem('hl-theme', next);
  } catch {
    /* ignore */
  }
  apply();
}

export function initTheme() {
  try {
    const saved = localStorage.getItem('hl-theme');
    if (saved === 'light' || saved === 'dark') mode = saved;
  } catch {
    /* ignore */
  }
  apply();
}
