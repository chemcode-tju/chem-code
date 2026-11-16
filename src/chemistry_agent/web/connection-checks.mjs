const prefix = 'chemistry.connection-check.';
export const checkLifetime = 15 * 60 * 1000;

/** Reopening settings may reuse a recent observation, never a live connection. */
export function readConnectionCheck(storage, key, identity, now = Date.now()) {
  try {
    const item = JSON.parse(storage.getItem(prefix + key));
    if (!item || item.identity !== identity || !Number.isFinite(item.checkedAt) ||
        !Array.isArray(item.paths) || !Array.isArray(item.models) || typeof item.authenticated !== 'boolean' ||
        now < item.checkedAt || now - item.checkedAt >= checkLifetime) return null;
    return item;
  } catch { return null; }
}

export function saveConnectionCheck(storage, key, identity, result, now = Date.now()) {
  // Store no messages, account information, credentials or raw probe output.
  const item = {identity, checkedAt:now, available:result.available === true,
    authenticated:result.authenticated === true,
    paths:(result.paths || []).filter(path => typeof path === 'string'),
    models:(result.models || []).map(model => ({id:model.id, name:model.name,
      efforts:model.efforts || [], default_effort:model.default_effort || ''}))};
  try { storage.setItem(prefix + key, JSON.stringify(item)); } catch {}
  return item;
}

export function clearConnectionCheck(storage, key) {
  try { storage.removeItem(prefix + key); } catch {}
}
