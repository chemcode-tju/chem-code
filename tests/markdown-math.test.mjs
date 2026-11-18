import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import test from 'node:test';
import {createMarkdownRenderer} from '../src/chemistry_agent/web/core.mjs';
import {linkedWorkspaceFile} from '../src/chemistry_agent/web/workspace-files.mjs';

const require=createRequire(import.meta.url);
const markdown=createMarkdownRenderer(require('../src/chemistry_agent/web/vendor/markdown-it.min.js'), {
  katex:require('../src/chemistry_agent/web/vendor/katex/katex.min.js'),
  texmath:require('../src/chemistry_agent/web/vendor/texmath/texmath.js'),
});

test('actual Antigravity reply renders formula, local file label and inline code',()=>{
  const html=markdown.render(String.raw`刚才计算得到的甲醇分子式是 $\text{CH}_4\text{O}$，测试标记文件 [connection-test.txt](file:///Users/researcher/work/connection-test.txt) 中写入的内容是 `+'`ANTIGRAVITY_OK`。');
  assert.match(html, /class="katex"/);
  assert.match(html, /<msub>/);
  assert.match(html, /data-local-file-path="\/Users\/researcher\/work\/connection-test.txt"[^>]*>connection-test.txt<\/span>/);
  assert.match(html, /<code>ANTIGRAVITY_OK<\/code>/);
  assert.doesNotMatch(html, /href="file:|\[connection-test.txt\]/);
});

test('dollar and bracket formulas work in blocks, tables and live partial replies',()=>{
  for(const source of [String.raw`$E=mc^2$`,String.raw`\(E=mc^2\)`,String.raw`\[E=mc^2\]`,'$$\nE=mc^2\n$$','| 物质 | 式 |\n|---|---|\n| 水 | $H_2O$ |'])
    assert.match(markdown.render(source),/class="katex"/);
  assert.match(markdown.renderPartial(String.raw`结果 $\text{CH}_4\text{O}$ **完成`), /<strong>完成<\/strong>/);
  assert.doesNotThrow(()=>markdown.renderPartial(String.raw`正在计算 $\frac{1}{`));
});

test('code, currency and malformed math are not executable',()=>{
  for(const source of ['`$E$`','```python\nvalue = "$E$"\n```','$5 and $10',String.raw`\$100`])
    assert.doesNotMatch(markdown.render(source),/class="katex"/);
  assert.doesNotThrow(()=>markdown.render(String.raw`$\frac{1}$`));
  for(const source of [String.raw`$\href{javascript:alert(1)}{x}$`,String.raw`$\htmlClass{bad}{x}$`,String.raw`$\includegraphics{https://example.com/pixel.png}$`])
    assert.doesNotMatch(markdown.render(source),/<a\b|<img\b|class="bad"/);
  assert.doesNotMatch(markdown.render('[bad](javascript:alert(1))'),/href="javascript/);
});

test('local links resolve only against registered current-session files',()=>{
  const file={name:'a b.xyz',path:'a b.xyz',source_path:'/Users/a/a b.xyz',download_url:'/api/files/123'};
  for(const href of ['file:///Users/a/a%20b.xyz','file://localhost/Users/a/a%20b.xyz','/Users/a/a%20b.xyz','a%20b.xyz','./a%20b.xyz','/api/files/123'])
    assert.equal(linkedWorkspaceFile(href,[file],'http://127.0.0.1:8768'),file);
  for(const href of ['https://evil.test/api/files/123','file://remote/Users/a/a%20b.xyz','file:///Users/a/credentials.json','file:///Users/b/a%20b.xyz','javascript:alert(1)'])
    assert.equal(linkedWorkspaceFile(href,[file],'http://127.0.0.1:8768'),undefined);
  assert.equal(linkedWorkspaceFile('file:///Users/a/a%20b.xyz',[],'http://127.0.0.1:8768'),undefined);
  const remote={...file,source_path:'/data/research/a b.xyz',sources:[{host:'cluster-a',path:'/data/research/a b.xyz'},{host:'local',path:'/Users/a/a b.xyz'}]};
  for(const href of ['file:///data/research/a%20b.xyz','/data/research/a%20b.xyz','file:///Users/a/a%20b.xyz'])
    assert.equal(linkedWorkspaceFile(href,[remote],'http://127.0.0.1:8768'),remote);
  assert.equal(linkedWorkspaceFile('/data/another/a%20b.xyz',[remote],'http://127.0.0.1:8768'),undefined);
  assert.match(markdown.render('[unknown](file:///private/not-published.txt)'), /<span class="local-file-reference"/);
  assert.doesNotMatch(markdown.render('`[x](file:///private/x)`'), /data-local-file-path/);
});
