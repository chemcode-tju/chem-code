'use strict';
const assert=require('node:assert/strict');
const test=require('node:test');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../../src/chemistry_agent/web/structure/i18n.js'),'utf8');
const catalog=require('../../src/chemistry_agent/web/structure/locales.json');

function viewer(language='zh-CN'){
  class Element {
    constructor(tag,attrs={},text){this.nodeType=1;this.tagName=tag;this.attrs={...attrs};this.childNodes=[];if(text!==undefined)this.append(new Text(text));}
    append(child){child.parentElement=this;this.childNodes.push(child);return child;}
    get firstChild(){return this.childNodes[0];}
    get textContent(){return this.childNodes.map(child=>child.nodeValue??child.textContent).join('');}
    set textContent(value){this.childNodes=[];this.append(new Text(value));}
    hasAttribute(key){return key in this.attrs;}
    getAttribute(key){return this.attrs[key]??null;}
    setAttribute(key,value){this.attrs[key]=value;}
    closest(selector){
      const excluded=(selector.includes('textarea')&&this.tagName==='textarea')||['script','style'].includes(this.tagName)
        ||this.hasAttribute('data-no-i18n')||this.attrs.id==='atom-list'
        ||(this.attrs.id==='filename'&&!this.hasAttribute('data-empty-title'));
      return excluded?this:this.parentElement?.closest(selector)||null;
    }
  }
  class Text {constructor(value){this.nodeType=3;this.nodeValue=value;}}
  const body=new Element('body');
  const languageLabel=body.append(new Element('label',{'data-no-i18n':''},'Language '));
  const select=languageLabel.append(new Element('select',{id:'language'}));
  select.options=[new Element('option',{},'简体中文'),new Element('option',{},'English')];
  const play=body.append(new Element('button',{'aria-label':'播放轨迹'},'播放轨迹'));
  const filename=body.append(new Element('h1',{id:'filename'},'结构与轨迹'));
  const protectedParent=body.append(new Element('div',{'data-no-i18n':''}));
  const data=protectedParent.append(new Element('span',{title:'打开文件或目录'},'打开文件或目录'));
  const textarea=body.append(new Element('textarea',{placeholder:'打开文件或目录'},'结构与轨迹'));
  textarea.value='结构与轨迹';
  const storage=new Map([['chemistry.language',language]]),writes=[];
  let observer;
  const events=new EventTarget();
  const context={document:{body,documentElement:{},getElementById:id=>id==='language'?select:null},Event,
    localStorage:{getItem:key=>storage.get(key)??null,setItem:(key,value)=>{writes.push([key,value]);storage.set(key,value);}},
    MutationObserver:class{constructor(callback){observer=callback;}observe(){}},
    addEventListener:events.addEventListener.bind(events),dispatchEvent:events.dispatchEvent.bind(events)};
  vm.runInNewContext(source,context);
  const storageEvent=(key,value)=>{if(value===null)storage.delete(key);else storage.set(key,value);const event=new Event('storage');event.key=key;event.newValue=value;events.dispatchEvent(event);};
  return {api:context.StructureI18n,context,body,Element,play,filename,data,textarea,select,storageEvent,writes,mutate:records=>observer(records)};
}

test('viewer follows shared language changes without storage echoes and persists explicit selections',()=>{
  const v=viewer();
  assert.equal(v.writes.length,0);
  v.storageEvent('chemistry.language','en');
  assert.equal(v.play.textContent,'Play trajectory');
  assert.equal(v.play.getAttribute('aria-label'),'Play trajectory');
  assert.equal(v.select.value,'en');
  assert.equal(v.context.document.title,'Structures & trajectories · Chem Code');
  assert.equal(v.writes.length,0);
  v.storageEvent('chemistry.language','zh-CN');
  assert.equal(v.play.textContent,'播放轨迹');
  assert.equal(v.play.getAttribute('aria-label'),'播放轨迹');
  assert.equal(v.context.document.title,'结构与轨迹 · Chem Code');
  v.api.setLanguage('en');
  assert.equal(v.writes.length,2);
  assert.deepEqual(v.writes.map(([key])=>key),['structure-viewer.language','chemistry.language']);
});

test('dynamic English UI returns to Chinese while filenames, nested data and input values stay intact',()=>{
  const v=viewer('en');
  const dynamic=v.body.append(new v.Element('button',{},v.api.translate('打开文件或目录')));
  const saved=v.body.append(new v.Element('span',{},v.api.translate('已保存：$&')));
  v.mutate([{type:'childList',addedNodes:[dynamic,saved]}]);
  v.textarea.setAttribute('placeholder','筛选粒子');
  v.mutate([{type:'attributes',target:v.textarea},{type:'attributes',target:v.data}]);
  assert.equal(v.textarea.getAttribute('placeholder'),'Filter particles');
  assert.equal(v.textarea.value,'结构与轨迹');
  assert.equal(v.filename.textContent,'结构与轨迹');
  assert.equal(v.data.getAttribute('title'),'打开文件或目录');
  assert.equal(v.data.textContent,'打开文件或目录');
  v.storageEvent('chemistry.language','zh-CN');
  assert.equal(dynamic.textContent,'打开文件或目录');
  assert.equal(saved.textContent,'已保存：$&');
  assert.equal(v.textarea.getAttribute('placeholder'),'筛选粒子');
  assert.equal(v.textarea.value,'结构与轨迹');
});

test('browser and Node catalogues match and shared preference overrides the viewer legacy preference',()=>{
  const v=viewer('en');
  assert.deepEqual(JSON.parse(JSON.stringify(v.api.catalog)),catalog);
  v.storageEvent('structure-viewer.language','zh-CN');
  assert.equal(v.api.language,'en');
  v.storageEvent('chemistry.language',null);
  assert.equal(v.api.language,'zh-CN');
});
