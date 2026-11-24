import {escapeHtml as esc} from './core.mjs';
import {t} from './i18n.mjs';

export function groupMessages(messages, markdown) {
  return messages.map(({member_id, name, answer, handoff}) => {
    const mention = member_id ? `<button type="button" class="group-mention" data-mention-member="${esc(member_id)}" data-member-name="${esc(name)}" title="${esc(t('点名') + ' ' + name)}" aria-label="${esc(t('点名') + ' ' + name)}" hidden>@</button>` : '';
    // Some providers use the handoff receipt itself as the final answer.
    const receiptOnly = handoff && answer === `交给 ${handoff.name}：${handoff.task}`;
    const body = receiptOnly ? '' : `<div class="markdown">${markdown.render(answer)}</div>`;
    const transfer = handoff ? `<details class="group-transfer"${receiptOnly ? ' open' : ''}><summary>${t('交给')} <span data-no-i18n>${esc(handoff.name)}</span><svg class="icon" aria-hidden="true"><use href="/assets/ui-icons.svg#i-chevron"/></svg></summary><div class="markdown">${markdown.render(handoff.task)}</div></details>` : '';
    return `<section class="group-speech"><header><strong data-no-i18n>${esc(name)}</strong>${mention}</header>${body}${transfer}</section>`;
  }).join('');
}

/** Replace only a real leading addressee. Keep the user's draft after it. */
export function addressDraft(draft, recipient, roster) {
  const text = draft.trimStart();
  const previous = roster.find(({name}) => text.slice(0, name.length + 1).toLowerCase() === '@' + name.toLowerCase()
    && /^(?:$|[\s,:，：])/u.test(text.slice(name.length + 1)));
  const body = previous ? text.slice(previous.name.length + 1).replace(/^[\s,:，：]+/u, '') : text;
  return '@' + recipient + ' ' + body;
}
