import {t} from './i18n.mjs';

const extensions = {
  c:'c', cc:'cpp', cpp:'cpp', cu:'cpp', css:'css', diff:'diff', f:'fortran', f90:'fortran',
  go:'go', h:'c', hpp:'cpp', html:'xml', ini:'ini', java:'java', jl:'julia', js:'javascript',
  json:'json', jsonl:'json', jsx:'javascript', m:'matlab', mjs:'javascript',
  md:'markdown', py:'python', pyi:'python', r:'r', rb:'ruby', rs:'rust', sh:'bash',
  slurm:'bash', sql:'sql', svg:'xml', tex:'latex', toml:'ini', ts:'typescript',
  tsx:'typescript', xml:'xml', yaml:'yaml', yml:'yaml', zsh:'bash', csv:'delimited', tsv:'delimited',
};

function languageFor(name) {
  const lower = name.toLowerCase();
  if (lower === 'makefile') return 'makefile';
  if (lower === 'dockerfile' || lower.startsWith('dockerfile.')) return 'dockerfile';
  if (['incar', 'kpoints'].includes(lower)) return 'ini';
  const extension = lower.includes('.') ? lower.split('.').at(-1) : '';
  return Object.hasOwn(extensions, extension) ? extensions[extension] : null;
}

let highlighter, parser;
function loadHighlighter() {
  return highlighter ??= import('./vendor/highlight/highlight.min.mjs').then(({default:hljs}) => {
    hljs.registerLanguage('delimited', hl => ({contains:[
      {scope:'string', begin:/"/, end:/"/, contains:[{match:/""/}]},
      {scope:'punctuation', match:/[,\t;]/}, hl.NUMBER_MODE,
    ]}));
    return hljs;
  });
}

function loadParser() {
  return parser ??= new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = '/assets/vendor/papaparse/papaparse.min.js';
    script.onload = () => resolve(window.Papa);
    script.onerror = () => {script.remove(); parser = null; reject(Error(t('无法加载表格预览。')));};
    document.head.append(script);
  });
}

const maxRows = 300;
const maxColumns = 80;

function renderTable(target, rows) {
  const table = document.createElement('table');
  table.className = 'file-preview-table';
  const columns = Math.min(maxColumns, Math.max(0, ...rows.map(row => row.length)));
  const body = table.createTBody();
  rows.forEach((values, index) => {
    const row = body.insertRow();
    const rowNumber = document.createElement('th');
    rowNumber.scope = 'row';
    rowNumber.textContent = index + 1;
    row.append(rowNumber);
    for (let column = 0; column < columns; column++) {
      const cell = row.insertCell();
      cell.textContent = values[column] ?? '';
    }
  });
  target.replaceChildren(table);
}

export function showFileText(target, name, data, signal) {
  const pre = document.createElement('pre');
  const code = document.createElement('code');
  code.textContent = data.text;
  const lines = data.text.replace(/(?:\r\n|\r|\n)$/, '').split(/\r\n|\r|\n/).length;
  if (lines <= 5000) {
    const gutter = document.createElement('span');
    gutter.className = 'file-preview-gutter';
    gutter.setAttribute('aria-hidden', 'true');
    gutter.textContent = Array.from({length:lines}, (_, index) => index + 1).join('\n');
    pre.append(gutter);
  }
  pre.append(code);
  const language = languageFor(name);
  const note = document.createElement('p');
  note.className = 'file-browser-message';
  note.textContent = t('仅预览前 256 KiB');
  note.hidden = !data.truncated;
  if (language === 'delimited') {
    const controls = document.createElement('div');
    controls.className = 'file-preview-controls';
    const content = document.createElement('div');
    const textButton = document.createElement('button');
    const tableButton = document.createElement('button');
    for (const [button, label] of [[textButton, '文本'], [tableButton, '表格']]) {
      button.type = 'button';
      button.textContent = t(label);
      button.setAttribute('aria-pressed', String(button === textButton));
      controls.append(button);
    }
    const tableNote = document.createElement('p');
    tableNote.className = 'file-browser-message';
    tableNote.hidden = true;
    let version = 0;
    textButton.onclick = () => {
      version++;
      textButton.setAttribute('aria-pressed', 'true');
      tableButton.setAttribute('aria-pressed', 'false');
      tableNote.hidden = true;
      content.replaceChildren(pre);
    };
    tableButton.onclick = async () => {
      const current = ++version;
      tableButton.setAttribute('aria-pressed', 'true');
      textButton.setAttribute('aria-pressed', 'false');
      tableNote.hidden = false;
      tableNote.textContent = t('正在读取表格…');
      try {
        const papa = await loadParser();
        if (signal.aborted || current !== version) return;
        const result = papa.parse(data.text, {delimiter:/\.tsv$/i.test(name) ? '\t' : '',
          preview:maxRows + 1});
        const rows = result.data;
        // The final record of a bounded excerpt can end inside a quoted field.
        if (rows.at(-1)?.length === 1 && rows.at(-1)[0] === '' && /[\r\n]$/.test(data.text)) rows.pop();
        if (data.truncated && rows.length <= maxRows && !result.meta.truncated) rows.pop();
        tableNote.hidden = rows.length <= maxRows && rows.every(row => row.length <= maxColumns);
        tableNote.textContent = t('表格最多显示 300 行、80 列。');
        renderTable(content, rows.slice(0, maxRows));
      } catch (error) {
        if (signal.aborted || current !== version) return;
        textButton.click();
        tableNote.hidden = false;
        tableNote.textContent = error.message;
      }
    };
    content.append(pre);
    target.replaceChildren(controls, note, tableNote, content);
  } else {
    target.replaceChildren(note, pre);
  }
  if (language && data.text.length <= 64 * 1024) {
    // Paint raw text immediately and decorate it once the grammar is available.
    void loadHighlighter().then(hljs => {
      if (!signal.aborted && hljs.getLanguage(language)) {
        code.innerHTML = hljs.highlight(data.text, {language, ignoreIllegals:true}).value;
      }
    }).catch(() => {});
  }
}
