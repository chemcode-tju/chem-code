import {escapeHtml} from './core.mjs';
import {t} from './i18n.mjs';

export const byId = id => document.getElementById(id);
export const esc = value => escapeHtml(String(value ?? ''));
export const field = (form,name) => form.elements.namedItem(name);
export const value = (form,name) => field(form,name).value.trim();
export const hasId = id => /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/.test(id);
export function remotePath(path) {
  if (!path.startsWith('/') || /[\n\r\0]/.test(path)) throw new Error(t('请输入服务器上的绝对路径。'));
  return path;
}
export async function computeRequest(path, method='GET', body) {
  const response=await fetch('/api/automation/'+path,{method,credentials:'same-origin',cache:'no-store',redirect:'error',
    ...(body===undefined ? {} : {headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})});
  const data=await response.json().catch(()=>null);
  if(!response.ok) {
    // A password endpoint may reject its input. Never echo its response body.
    if(path.endsWith('/password')) throw new Error(t('钥匙串操作失败，请检查服务所在 Mac 上的钥匙串权限后重试。'));
    if(response.status===401) throw new Error(t('请先登录工作空间。'));
    const reason=Array.isArray(data?.detail) ? data.detail.map(row=>row.msg).filter(Boolean).join(' · ') : '';
    throw new Error(data?.error?.message || reason || (response.status===404 ? t('当前服务未启用计算设置。') : t('请求失败，请重试。')));
  }
  return data;
}
export function facts(rows) {
  return '<dl class="config-facts">'+rows.filter(([,v])=>v!==null && v!==undefined && v!=='').map(([k,v])=>
    `<dt>${esc(t(k))}</dt><dd data-no-i18n>${esc(v)}</dd>`).join('')+'</dl>';
}
export function record(title,rows,action='') {
  return `<article class="config-record"><header><strong data-no-i18n>${esc(title)}</strong>${action}</header>${facts(rows)}</article>`;
}
export const empty = label => `<p class="config-empty">${esc(t(label))}</p>`;
export function replaceOptions(select, rows, placeholder) {
  const current=select.value;
  select.replaceChildren(new Option(t(placeholder),''),...rows.map(([id,label])=>new Option(label,id)));
  if(rows.some(([id])=>id===current)) select.value=current;
  else if(rows.length===1) select.value=rows[0][0];
}
