import {escapeHtml as esc} from './core.mjs';
import {t} from './i18n.mjs';

// Keep edits when the chat refreshes (progress events can render it again).
export function userQuestions({root, session, api, sync, toast}) {
  const drafts = new Map(), sending = new Set();
  function read(form) {
    return Array.from(form.querySelectorAll('fieldset'), field => ({
      id:field.dataset.item,
      selected:Array.from(field.querySelectorAll('input:checked'), input=>input.value),
      custom:field.querySelector('textarea').value.trim(),
    }));
  }
  root.addEventListener('input', event=>{
    const form=event.target.closest('form[data-question]');
    if(form) drafts.set(form.dataset.question,read(form));
  });
  root.addEventListener('submit', async event=>{
    const form=event.target.closest('form[data-question]');
    if(!form) return;
    event.preventDefault();
    const id=form.dataset.question, owner=session(), answers=read(form);
    if(!owner || owner!==form.dataset.session || sending.has(id)) return;
    if(answers.some(a=>!a.selected.length && !a.custom)) {
      toast(t('请选择选项或填写回答。')); return;
    }
    sending.add(id);form.querySelector('button').disabled=true;
    try {
      await api('/sessions/'+encodeURIComponent(owner)+'/questions/'+encodeURIComponent(id)+'/answer',
        {method:'POST',body:JSON.stringify({answers})});
      drafts.delete(id);
      if(session()===owner) await sync();
    } catch(error) { toast(error.message); }
    finally { sending.delete(id); if(form.isConnected) form.querySelector('button').disabled=false; }
  });
  return function render(result, active) {
    if(!active) {
      return result.questions.map(q=>`<p class="past-question" data-no-i18n>${esc(q.question)}</p>`).join('');
    }
    const id=result.question_id, saved=drafts.get(id) || [];
    return `<form class="user-questions" data-question="${esc(id)}" data-session="${esc(session())}">
      ${result.questions.map((q,i)=>{
        const previous=saved.find(a=>a.id===q.id);
        return `<fieldset data-item="${esc(q.id)}" ><legend data-no-i18n>${esc(q.question)}</legend>
          ${q.options.map(o=>`<label class="question-option"><input type="${q.multi_select?'checkbox':'radio'}" name="q-${esc(id)}-${i}" value="${esc(o.label)}" ${previous?.selected.includes(o.label)?'checked':''}><span data-no-i18n>${esc(o.label)}${o.description?`<small>${esc(o.description)}</small>`:''}</span></label>`).join('')}
          <textarea rows="1" maxlength="1800" aria-label="${esc(t('其他回答…'))}" placeholder="${esc(t('其他回答…'))}">${esc(previous?.custom || '')}</textarea></fieldset>`;
      }).join('')}
      <button type="submit" ${sending.has(id)?'disabled':''}>${esc(t('发送选择'))}</button></form>`;
  };
}
