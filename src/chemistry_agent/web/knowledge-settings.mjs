import {t, translateUI} from './i18n.mjs';
import {readConnectionCheck,saveConnectionCheck,clearConnectionCheck} from './connection-checks.mjs';

export function createKnowledgeSettings(api) {
  const $ = id => document.getElementById(id);
  let loaded = false;
  let storage;
  try {storage=window.sessionStorage;} catch {}
  const identity=config=>JSON.stringify([config.endpoint,config.root_uri,config.has_api_key]);
  const status = (message, error = false) => {
    $('knowledge-notice').textContent = message;
    $('knowledge-notice').classList.toggle('error', error);
  };
  async function action(button, operation) {
    if (button.disabled) return;
    button.disabled = true; status(t('正在处理…'));
    try { await operation(); }
    catch (error) { status(error.message, true); }
    finally { button.disabled = false; }
  }
  function connectionState(config) {
    const check=config.enabled && readConnectionCheck(storage,'openviking',identity(config));
    $('ov-state').textContent = t(!config.enabled?'未启用':check?(check.authenticated?'上次测试通过':'上次测试失败'):'已启用 · 尚未检测');
    $('ov-state').title=check?new Date(check.checkedAt).toLocaleString():'';
    $('ov-probe').disabled = !config.enabled;
    $('ov-sync').disabled = !config.enabled;
  }
  function show(config) {
    $('ov-enabled').checked = config.enabled;
    $('ov-endpoint').value = config.endpoint;
    $('ov-root').value = config.root_uri;
    $('ov-key').value = '';
    $('ov-key').placeholder = t(config.has_api_key ? '已保存（留空保留）' : 'API Key');
    connectionState(config);
  }
  function render(packages) {
    $('skill-packages').replaceChildren();
    for (const pkg of packages) {
      const card = document.createElement('details'); card.className = 'agent-row';
      const summary = document.createElement('summary');
      const identity = document.createElement('span'); identity.className = 'agent-identity';
      const title = document.createElement('strong'); title.textContent = pkg.name; title.dataset.noI18n = '';
      const source = document.createElement('span'); source.className = 'agent-model';
      source.textContent = `${pkg.skills.length} ${t('Skills')} · ${pkg.revision.slice(0,12)}`;
      source.dataset.noI18n = ''; identity.append(title, source);
      const state = document.createElement('span'); state.textContent = t(pkg.enabled ? '已启用' : '未启用');
      summary.append(identity, state); card.append(summary);
      const body = document.createElement('div'); body.className = 'agent-fields';
      const origin = document.createElement('p'); origin.textContent = pkg.source; origin.dataset.noI18n = '';
      origin.className = 'plugin-source'; body.append(origin);
      const toggle = document.createElement('button'); toggle.type = 'button'; toggle.className = 'secondary';
      toggle.textContent = t(pkg.enabled ? '停用插件' : '启用插件');
      toggle.onclick = () => action(toggle, async () => {
        await api(`/api/knowledge/plugins/${pkg.id}`, {method:'PUT', body:JSON.stringify({enabled:!pkg.enabled})});
        await refreshPlugins(); status(t('已保存'));
      }); body.append(toggle);
      for (const skill of pkg.skills) {
        const detail = document.createElement('details'); detail.className = 'config-editor';
        const label = document.createElement('summary'); label.textContent = skill.name; label.dataset.noI18n = '';
        const description = document.createElement('p'); description.textContent = skill.description; description.dataset.noI18n = '';
        const preview = document.createElement('pre'); preview.className = 'skill-preview'; preview.dataset.noI18n = '';
        detail.append(label, description, preview);
        detail.addEventListener('toggle', async () => {
          if (!detail.open || preview.textContent || !pkg.enabled) return;
          try {
            const value = await api('/api/knowledge/skills/read', {method:'POST', body:JSON.stringify({skill:`${pkg.id}/${skill.key}`})});
            preview.textContent = value.content;
          } catch (error) { preview.textContent = error.message; }
        }); body.append(detail);
      }
      card.append(body); $('skill-packages').append(card);
    }
    $('skills-empty').hidden = Boolean(packages.length);
    translateUI();
  }
  async function refreshPlugins() { render((await api('/api/knowledge/plugins')).packages); }
  $('ov-form').addEventListener('submit', event => {
    event.preventDefault();
    action($('ov-save'), async () => {
      const config = await api('/api/knowledge/settings', {method:'PUT', body:JSON.stringify({
        enabled:$('ov-enabled').checked, endpoint:$('ov-endpoint').value.trim(),
        api_key:$('ov-key').value || null, root_uri:$('ov-root').value.trim(),
      })}); clearConnectionCheck(storage,'openviking');show(config); status(t('已保存'));
    });
  });
  $('ov-clear-key').onclick = () => action($('ov-clear-key'), async () => {
    const config = await api('/api/knowledge/settings');
    delete config.has_api_key;
    show(await api('/api/knowledge/settings', {method:'PUT', body:JSON.stringify({...config, clear_key:true})}));
    status(t('已保存'));
  });
  $('ov-probe').onclick = () => action($('ov-probe'), async () => {
    const config=await api('/api/knowledge/settings');
    try {
      await api('/api/knowledge/probe', {method:'POST'});
      saveConnectionCheck(storage,'openviking',identity(config),{authenticated:true});status(t('连接成功'));
    } catch(error) {
      saveConnectionCheck(storage,'openviking',identity(config),{authenticated:false});throw error;
    } finally {connectionState(config);}
  });
  $('ov-sync').onclick = () => action($('ov-sync'), async () => {
    const result = await api('/api/knowledge/sync', {method:'POST'});
    status(t('已同步 {0} 条记忆', result.updated));
  });
  $('plugin-form').addEventListener('submit', event => {
    event.preventDefault();
    action($('plugin-install'), async () => {
      await api('/api/knowledge/plugins', {method:'POST', body:JSON.stringify({
        source:$('plugin-source').value.trim(), ref:$('plugin-ref').value.trim() || 'HEAD',
        plugin:$('plugin-name').value.trim() || null,
      })}); await refreshPlugins(); status(t('插件已安装'));
    });
  });
  return {async activate(tab) {
    if (tab !== 'knowledge' || loaded) return;
    try { show(await api('/api/knowledge/settings')); await refreshPlugins(); loaded = true; }
    catch (error) { status(error.message, true); }
  }};
}
