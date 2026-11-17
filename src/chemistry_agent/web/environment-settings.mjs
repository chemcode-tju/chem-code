import {t, translateUI} from './i18n.mjs';

export function createEnvironmentSettings(api) {
  const $ = id => document.getElementById(id);
  let records = [], loaded = false;
  const notify = message => { $('runner-notice').textContent = message; };
  function edit(record = {}) {
    $('runner-id').value = record.id || ''; $('runner-id').readOnly = Boolean(record.id);
    $('runner-name').value = record.name || '';
    $('runner-transport').value = record.transport || 'runner';
    $('runner-endpoint').value = record.endpoint || 'http://127.0.0.1:8767';
    $('runner-token').value = ''; $('runner-token').placeholder = t(record.has_token ? '已保存（留空保留）' : '连接令牌');
    $('runner-capabilities').value = (record.capabilities || []).join(', ');
    $('runner-enabled').checked = record.enabled !== false;
    $('runner-editor').open = true; connectionFields();
  }
  function connectionFields() {
    $('runner-connection').hidden = $('runner-transport').value === 'local';
  }
  async function refresh() {
    const data = await api('/api/environments'); records = data.environments;
    $('runtime-host').textContent = data.platform_host;
    $('runner-list').replaceChildren();
    for (const record of records) {
      const row = document.createElement('div'); row.className = 'environment-row';
      const main = document.createElement('button'); main.type = 'button'; main.className = 'environment-edit secondary';
      const title = document.createElement('strong'); title.textContent = record.name; title.dataset.noI18n = '';
      const info = document.createElement('span'); info.textContent = `${record.id} · ${record.transport} · ${record.capabilities.join(' / ')}`;
      info.dataset.noI18n = ''; main.append(title, info); main.onclick = () => edit(record);
      const state = document.createElement('span'); state.textContent = t(record.enabled ? '已启用' : '未启用');
      const probe = document.createElement('button'); probe.type = 'button'; probe.className = 'secondary';
      probe.textContent = t('检测连接'); probe.disabled = !record.enabled;
      probe.onclick = async () => {
        probe.disabled = true;
        try {
          const value = await api(`/api/environments/${record.id}/probe`, {method:'POST'});
          notify(`${record.name} · ${t('上次测试通过')} · ${value.name} · ${new Date().toLocaleTimeString()}`);
        } catch (error) { notify(error.message); }
        finally { probe.disabled = !record.enabled; }
      };
      row.append(main, state, probe); $('runner-list').append(row);
    }
    translateUI();
  }
  $('runner-new').onclick = () => edit();
  $('runner-transport').onchange = connectionFields;
  $('runner-form').onsubmit = async event => {
    event.preventDefault(); $('runner-save').disabled = true;
    try {
      const id = $('runner-id').value.trim();
      await api(`/api/environments/${encodeURIComponent(id)}`, {method:'PUT', body:JSON.stringify({
        id, name:$('runner-name').value.trim(), transport:$('runner-transport').value,
        endpoint:$('runner-endpoint').value.trim(), token:$('runner-token').value || null,
        enabled:$('runner-enabled').checked,
        capabilities:$('runner-capabilities').value.split(',').map(s=>s.trim()).filter(Boolean),
      })}); $('runner-editor').open = false; await refresh(); notify(t('已保存'));
    } catch (error) { notify(error.message); }
    finally { $('runner-save').disabled = false; }
  };
  return {async activate(tab) {
    if (tab !== 'servers' || loaded) return;
    try { await refresh(); loaded = true; } catch (error) { notify(error.message); }
  }};
}
