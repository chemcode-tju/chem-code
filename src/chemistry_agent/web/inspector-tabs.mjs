/** Wire the inspector tabs to the host's desktop dock / mobile panel opener. */
export function initInspectorTabs(openInspector) {
  const tabs = [...document.querySelectorAll('#inspector [data-inspector-tab]')];
  const frame = document.getElementById('structure-viewer-frame');

  function selectInspectorTab(name) {
    const selected = tabs.find((tab) => tab.dataset.inspectorTab === name);
    if (!selected) return;
    openInspector();
    for (const tab of tabs) {
      const active = tab === selected;
      tab.setAttribute('aria-selected', String(active));
      tab.tabIndex = active ? 0 : -1;
      document.getElementById(tab.getAttribute('aria-controls')).hidden = !active;
    }
    if (name === 'structure' && !frame.hasAttribute('src')) frame.src = frame.dataset.src;
    selected.focus({ preventScroll: true });
  }

  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => selectInspectorTab(tab.dataset.inspectorTab));
    tab.addEventListener('keydown', (event) => {
      const destinations = {
        ArrowLeft: (index + tabs.length - 1) % tabs.length,
        ArrowRight: (index + 1) % tabs.length,
        Home: 0,
        End: tabs.length - 1,
      };
      if (!Object.hasOwn(destinations, event.key)) return;
      event.preventDefault();
      selectInspectorTab(tabs[destinations[event.key]].dataset.inspectorTab);
    });
  });
  document.querySelectorAll('[data-open-inspector]').forEach((button) => {
    button.addEventListener('click', () => selectInspectorTab(button.dataset.openInspector));
  });
  return selectInspectorTab;
}
