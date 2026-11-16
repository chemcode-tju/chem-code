import {escapeHtml as esc} from './core.mjs';

// Edits survive status refreshes. Revision conflicts never replace the user's text.
export function createTextEditor(host,{label,direct=false,prose=false,save}) {
  host.classList.add('step-text-editor');
  const icon=(action,title,name)=>`<button type="button" class="icon-button" data-editor="${action}" title="${esc(title)}" aria-label="${esc(title)}"><svg class="icon" aria-hidden="true"><use href="/assets/ui-icons.svg#i-${name}"/></svg></button>`;
  host.innerHTML=`<div class="step-editor-toolbar">${icon('edit','编辑 '+label,'pencil')}${icon('save','保存 '+label,'save')}${icon('cancel','取消编辑 '+label,'close')}</div><p class="step-prose" hidden></p><textarea aria-label="${esc(label)}" spellcheck="false" readonly></textarea><p class="step-editor-message" role="status" hidden></p>`;
  const area=host.querySelector('textarea'), preview=host.querySelector('.step-prose'), message=host.querySelector('[role=status]');
  const button=name=>host.querySelector(`[data-editor="${name}"]`);
  let text='', revision=null, editRevision=null, editable=false, editing=false, busy=false;
  const dirty=()=>area.value!==text;
  function draw() {
    const active=editing || (direct && editable);
    area.readOnly=!active || !editable || busy;
    area.hidden=prose && !active;
    preview.hidden=!prose || active || (!text && !editable);preview.textContent=text || '尚未填写步骤说明。';
    host.hidden=prose && !text && !editable && !active;
    button('edit').hidden=direct || editing || !editable;
    button('save').hidden=!active;
    button('save').disabled=busy || !editable || !dirty();
    button('cancel').hidden=!active || (!editing && !dirty());
    button('cancel').disabled=busy;
    host.dataset.editing=String(active);
    area.rows=prose?Math.min(14,Math.max(4,area.value.split('\n').length+2)):Math.min(22,Math.max(6,area.value.split('\n').length+1));
  }
  button('edit').onclick=()=>{editing=true;editRevision=revision;message.hidden=true;draw();area.focus();};
  button('cancel').onclick=()=>{editing=false;area.value=text;editRevision=null;message.hidden=true;draw();};
  area.oninput=()=>{if(editRevision===null)editRevision=revision;message.hidden=true;draw();};
  button('save').onclick=async()=>{
    if(busy || !editable || !dirty())return;
    busy=true;draw();
    try {
      const value=area.value;
      await save(value,editRevision ?? revision);
      text=value;area.value=value;editing=false;editRevision=null;
      message.textContent='已保存';message.hidden=false;
    } catch(error) {message.textContent=error.message;message.hidden=false;}
    finally {busy=false;draw();}
  };
  return {
    update(next) {
      const untouched=!dirty() && !editing;
      editable=Boolean(next.editable);revision=next.revision;
      if(untouched) {text=next.text || '';area.value=text;}
      area.title=editable?'':next.reason || '';
      draw();
    },
    get dirty() {return dirty();},
  };
}
