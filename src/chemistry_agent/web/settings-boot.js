// Run before styles and deferred modules: the first painted layout is the final one.
(() => {
  const root = document.documentElement;
  let embedded = false;
  if (parent !== window && new URLSearchParams(location.search).get('embed') === '1') {
    try { embedded = parent.location.origin === location.origin; } catch { /* Foreign host. */ }
  }
  root.classList.toggle('embedded-settings', embedded);
  root.classList.toggle('compact-settings', (embedded ? parent : window).matchMedia('(max-width:767px)').matches);
  root.dataset.settingsState = 'loading';
  const english = root.lang === 'en';
  function status(failed = false) {
    const label = document.getElementById('settings-loading-label');
    if (label) label.textContent = failed
      ? (english ? 'Unable to load settings.' : '设置加载失败。')
      : (english ? 'Opening settings…' : '正在打开设置…');
    const retry = document.getElementById('settings-retry');
    if (retry) { retry.hidden = !failed; retry.textContent = english ? 'Reload' : '重新加载'; }
  }
  const timeout = setTimeout(() => {
    if (root.dataset.settingsState !== 'ready') { root.dataset.settingsState = 'error'; status(true); }
  }, 12000);
  document.addEventListener('readystatechange', () => status(root.dataset.settingsState === 'error'));
  document.addEventListener('click', event => {
    if (event.target.closest?.('#settings-retry')) location.reload();
  });
  window.chemistrySettingsReady = () => {
    clearTimeout(timeout);
    root.dataset.settingsState = 'ready';
  };
})();
