import { api } from './api.js';

/** Who is signed in (null = anonymous). Shared by the header and pages. */
export const session = $state({ user: null, loaded: false });

export async function refreshSession() {
  try {
    session.user = await api('/api/v1/me');
  } catch {
    session.user = null;
  } finally {
    session.loaded = true;
  }
}
