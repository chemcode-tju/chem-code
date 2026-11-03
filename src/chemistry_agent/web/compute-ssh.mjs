import {t} from './i18n.mjs';
import {byId as $, computeRequest, empty, esc, field, hasId, record, remotePath, value} from './compute-settings-api.mjs';

const modeNames={alias:'OpenSSH 别名',key:'私钥或 SSH Agent',password:'用户名和密码'};
const checkNames={ssh:'SSH 登录',scheduler:'调度系统',workdir_parent:'工作目录权限'};

export function serverPayload(form) {
  const mode=value(form,'auth_mode'), label=value(form,'label'), hostname=value(form,'hostname');
  if(!hasId(label)) throw new Error(t('连接名称应使用字母、数字、点、横线或下划线。'));
  if(!hostname || hostname.startsWith('-') || /[\s/@\\]/.test(hostname)) throw new Error(t('请输入 SSH 别名、主机名或 IP 地址。'));
  const workdir=remotePath(value(form,'workdir'));
  if(/[{}]/.test(workdir)) throw new Error(t('工作目录应是完整路径。'));
  const payload={label,hostname,workdir,scheduler:value(form,'scheduler'),auth_mode:mode};
  if(mode==='alias') return payload;
  const username=value(form,'username'), port=value(form,'port');
  if(!username) throw new Error(t('请输入 SSH 用户名。'));
  Object.assign(payload,{username});
  if(port) {
    if(!/^\d+$/.test(port) || +port<1 || +port>65535) throw new Error(t('SSH 端口必须在 1 到 65535 之间。'));
    payload.port=Number(port);
  }
  if(mode==='key') {
    const identity=value(form,'identity_file');
    if(identity && (!/^(?:~|\/.*)\/\.ssh\/[^/]/.test(identity) || /(?:^|\/)\.\.(?:\/|$)|\.pub$|[\r\n\0]|PRIVATE KEY/i.test(identity)))
      throw new Error(t('请填写服务所在 Mac 的 .ssh 目录下的私钥文件路径，或留空使用 ssh-agent。'));
    if(identity) payload.identity_file=identity;
  }
  return payload;
}

export function createSSHSettings({run,notify,afterSave,activeServer}) {
  const form=$('server-form'), dialog=$('ssh-secret');
  const receipts={new:null,existing:null}, tests=new Map();
  let selected=null, secretId=null;
  const box=kind=>$(kind==='new'?'new-trust':'existing-trust');
  function invalidate(kind) {
    receipts[kind]=null;box(kind).hidden=true;box(kind).replaceChildren();
    if(kind==='new') $('server-save').disabled=true;
  }
  function modeChanged() {
    const mode=value(form,'auth_mode');
    for(const [selector,visible] of [['[data-direct]',mode!=='alias'],['[data-key]',mode==='key']]) {
      for(const label of form.querySelectorAll(selector)) {
        label.hidden=!visible;label.querySelector('input').disabled=!visible;
      }
    }
    field(form,'username').required=mode!=='alias';
    $('host-label').textContent=t(mode==='alias'?'SSH 别名':'主机名或 IP');
    $('auth-mode-hint').textContent=t({
      alias:'使用服务所在 Mac 的 ~/.ssh/config。用户名、端口和身份文件沿用该别名的配置。',
      key:'公钥应已加入远端账号的 authorized_keys。私钥留在服务所在 Mac 上，只填写文件路径；留空使用 ssh-agent。',
      password:'先核对主机并保存服务器，再在独立密码表单中保存登录密码。'
    }[mode]);
    field(form,'hostname').placeholder=mode==='alias'?'cluster':'cluster.example.org';
  }
  function existingPayload() {
    const s=activeServer();if(!s) throw new Error(t('请先选择服务器。'));
    const mode=s.auth_mode || 'alias';
    const payload={label:s.label,auth_mode:mode,hostname:s.ssh_host || s.hostname,
      scheduler:(s.scheduler || 'slurm').replace(/^core\./,''),workdir:s.workdir};
    if(mode!=='alias') {
      if(s.username) payload.username=s.username;
      if(s.port) payload.port=s.port;
      if(mode==='key' && s.identity_file) payload.identity_file=s.identity_file;
    }
    return payload;
  }
  const draft=kind=>kind==='new' ? serverPayload(form) : existingPayload();
  function matches(kind,receipt) {
    try {return receipt && receipt.key===JSON.stringify(draft(kind)) && (kind==='new' || receipt.serverId===activeServer()?.id);}
    catch {return false;}
  }
  function receiptView(kind) {
    const receipt=receipts[kind], el=box(kind);
    if(!matches(kind,receipt)) {invalidate(kind);return;}
    const {result}=receipt, changed=result.changed===true, trusted=result.trusted===true && !changed;
    const canTrust=!trusted && (!changed || Boolean(result.previous_fingerprint));
    const message=trusted?'主机指纹已受信任。':changed && !canTrust
      ?'主机指纹与本机 known_hosts 冲突。请先向管理员核实并在本机更新原记录，再重新检查。'
      :changed?'主机指纹已变化。请通过独立渠道与管理员核实新指纹，再明确替换原信任。':'请通过独立渠道与管理员核对完整的 SHA256 指纹，一致后再信任。';
    el.hidden=false;
    el.innerHTML=`<strong data-no-i18n>${esc(result.hostname)}:${esc(result.port)}</strong><p>${esc(t(message))}</p>
      ${changed && result.previous_fingerprint?`<small>${esc(t('原指纹'))}</small><code data-no-i18n>${esc(result.previous_fingerprint)}</code>`:''}
      <small>${esc(t('当前指纹'))}</small><code data-no-i18n>${esc(result.fingerprint)}</code>
      ${canTrust?`<button type="button" data-trust="${kind}">${esc(t(changed?'已核对新指纹，替换原信任':'指纹一致，信任主机'))}</button>`:''}`;
    if(kind==='new') $('server-save').disabled=!trusted;
  }
  async function probe(kind) {
    const payload=draft(kind);
    invalidate(kind);
    const receipt={key:JSON.stringify(payload),serverId:activeServer()?.id};
    const result=await computeRequest('ssh/probe','POST',payload);
    if(!matches(kind,receipt)) return;
    if(!result || !/^SHA256:[A-Za-z0-9+/]{43}$/.test(result.fingerprint || '') || !result.hostname || !result.port)
      throw new Error(t('未取得有效的主机指纹，请重新检查。'));
    receipts[kind]={...receipt,result};receiptView(kind);
  }
  async function trust(kind) {
    const receipt=receipts[kind];
    if(!matches(kind,receipt)) throw new Error(t('连接信息已变化，请重新检查指纹。'));
    const {result}=receipt;
    if(result.trusted && !result.changed || result.changed && !result.previous_fingerprint) return;
    const payload=draft(kind);
    // Re-probe after trust: the key may have changed between the two requests.
    invalidate(kind);
    await computeRequest('ssh/trust','POST',{server:payload,fingerprint:result.fingerprint,
      ...(result.changed?{previous_fingerprint:result.previous_fingerprint}:{})});
    if(matches(kind,receipt)) await probe(kind);
  }
  function closeSecret() {
    $('ssh-password').value='';secretId=null;
    if(dialog.open) dialog.close();
  }
  function openSecret(server) {
    if(server?.auth_mode!=='password') return;
    secretId=server.id;$('ssh-password').value='';
    $('ssh-secret-server').textContent=[server.label,server.hostname].filter(Boolean).join(' · ');
    $('secret-notice').textContent='';$('secret-removal').open=false;
    dialog.showModal();$('ssh-password').focus();
  }
  function secretAction(remove) {
    const id=secretId;
    if(!id || activeServer()?.id!==id) {closeSecret();return;}
    const password=$('ssh-password').value;
    $('ssh-password').value='';
    run(async()=>{
      try {
        const path='servers/'+encodeURIComponent(id)+'/password';
        if(remove) await computeRequest(path,'DELETE');
        else await computeRequest(path,'PUT',{password});
      } catch(error) {
        if(dialog.open && secretId===id) $('secret-notice').textContent=error.message;else notify(error.message,true);
        return;
      }
      tests.delete(id);closeSecret();
      await afterSave(remove?'SSH 密码已清除。':'SSH 密码已保存，可测试连接。',id);
    });
  }
  function showChecks(id) {
    const checks=tests.get(id);
    $('server-checks').innerHTML=!checks ? '' : `<ul class="check-list">${checks.map(row=>
      `<li><span>${esc(t(checkNames[row.check] || row.check))}</span><b>${esc(t(row.ok?'通过':'未通过'))}</b><code data-no-i18n>${esc(row.detail)}</code></li>`).join('')}</ul>`;
  }
  function render(server) {
    if(selected!==server?.id) {
      invalidate('existing');invalidate('new');closeSecret();form.reset();modeChanged();
      $('new-server').open=false;selected=server?.id;
    }
    if(!server) {
      $('server-record').innerHTML=empty('还没有计算服务器。');$('server-actions').hidden=true;showChecks(null);return;
    }
    const mode=server.auth_mode || 'alias';
    $('server-record').innerHTML=record(server.label,[
      ['主机',server.hostname],['登录方式',t(modeNames[mode])],['SSH 别名',mode==='alias'?(server.ssh_host || server.hostname):''],
      ['Agent 终端别名',server.shell_alias],
      ['上次连接测试',server.last_connection_test ? t(server.last_connection_test.ok?'通过':'未通过')+' · '+new Date(server.last_connection_test.checked_at).toLocaleString() : t('尚未测试连接')],
      ['SSH 用户名',server.username],['SSH 端口',server.port],['本机私钥路径',mode==='key'?(server.identity_file || 'ssh-agent'):''],
      ['调度系统',server.scheduler],['远端工作目录',server.workdir]]);
    $('server-actions').hidden=false;$('password-open').hidden=mode!=='password';showChecks(server.id);
  }
  form.addEventListener('input',()=>{invalidate('new');if(field(form,'auth_mode')===document.activeElement) modeChanged();});
  field(form,'auth_mode').addEventListener('change',()=>{invalidate('new');modeChanged();});
  form.onsubmit=e=>{e.preventDefault();run(()=>probe('new'));};
  for(const kind of ['new','existing']) box(kind).onclick=e=>{if(e.target.closest('[data-trust]'))run(()=>trust(kind));};
  $('existing-probe').onclick=()=>run(()=>probe('existing'));
  $('server-save').onclick=()=>run(async()=>{
    const receipt=receipts.new;
    if(!matches('new',receipt) || receipt.result.changed || !receipt.result.trusted)
      throw new Error(t('先检查并核对主机指纹。'));
    const payload=draft('new');
    const saved=await computeRequest('servers','POST',payload);
    invalidate('new');form.reset();modeChanged();$('new-server').open=false;
    const refreshed=await afterSave('服务器已保存。',saved?.id);
    if(refreshed && payload.auth_mode==='password' && activeServer()?.id===saved?.id && activeServer()) openSecret(activeServer());
  });
  $('server-test').onclick=()=>run(async()=>{
    const id=activeServer().id;tests.delete(id);showChecks(id);notify('正在检查 SSH、调度系统和工作目录…');
    let result;
    try {
      result=await computeRequest('servers/'+encodeURIComponent(id)+'/test','POST');
    } catch(error) {
      if(activeServer()?.id===id) {
        activeServer().last_connection_test={ok:false,checked_at:new Date().toISOString()};
        render(activeServer());
      }
      throw error;
    }
    const checks=Array.isArray(result.checks)?result.checks:[];
    if(!checks.length) throw new Error(t('未收到连接检查结果。'));
    tests.set(id,checks);
    if(activeServer()?.id===id) {
      const ok=checks.every(c=>c.ok);
      activeServer().last_connection_test={ok,checked_at:result.checked_at || new Date().toISOString()};
      render(activeServer());
      notify(ok?'连接检查通过。':'有检查项未通过。');
    }
  });
  $('password-open').onclick=()=>openSecret(activeServer());
  $('ssh-secret-form').onsubmit=e=>{e.preventDefault();secretAction(false);};
  $('secret-clear').onclick=()=>secretAction(true);$('secret-cancel').onclick=closeSecret;
  dialog.addEventListener('close',()=>{if(!dialog.open){$('ssh-password').value='';secretId=null;}});
  window.addEventListener('pagehide',closeSecret);
  modeChanged();
  return {render,closeSecret,translate() {
    modeChanged();
    for(const kind of ['new','existing']) if(receipts[kind]) receiptView(kind);
  }};
}
