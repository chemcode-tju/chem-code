import assert from 'node:assert/strict';
import test from 'node:test';
import {readConnectionCheck,saveConnectionCheck,clearConnectionCheck,checkLifetime} from '../src/chemistry_agent/web/connection-checks.mjs';

const storage=()=>{
  const values=new Map();
  return {getItem:key=>values.get(key) ?? null,setItem:(key,value)=>values.set(key,value),removeItem:key=>values.delete(key)};
};

test('recent probe survives settings recreation only for the same configured identity',()=>{
  const data=storage(),time=100000;
  saveConnectionCheck(data,'codex','codex',{available:true,authenticated:true,paths:['codex','/opt/bin/codex'],models:[{id:'research',name:'Research',efforts:['high']}]},time);
  const reopened=readConnectionCheck(data,'codex','codex',time+1000);
  assert.equal(reopened.authenticated,true);
  assert.deepEqual(reopened.models[0].efforts,['high']);
  assert.equal(readConnectionCheck(data,'codex','/other/codex',time+1000),null);
  assert.equal(readConnectionCheck(data,'codex','codex',time+checkLifetime),null);
  assert.equal(readConnectionCheck(data,'codex','codex',time-1),null);
});

test('cached observation excludes raw output, account and credentials and can be invalidated',()=>{
  const data=storage();
  const saved=saveConnectionCheck(data,'ssh:cluster','identity',{available:true,authenticated:false,message:'sensitive output',account:{email:'private'},token:'private'});
  assert.equal(saved.authenticated,false);
  assert.equal('message' in saved,false);
  assert.equal('account' in saved,false);
  assert.equal('token' in saved,false);
  clearConnectionCheck(data,'ssh:cluster');
  assert.equal(readConnectionCheck(data,'ssh:cluster','identity'),null);
});

test('blocked or corrupt storage never prevents opening settings',()=>{
  const broken={getItem(){throw Error('blocked');},setItem(){throw Error('blocked');},removeItem(){throw Error('blocked');}};
  assert.equal(readConnectionCheck(broken,'codex','codex'),null);
  assert.equal(saveConnectionCheck(broken,'codex','codex',{authenticated:true}).authenticated,true);
  assert.doesNotThrow(()=>clearConnectionCheck(broken,'codex'));
  assert.equal(readConnectionCheck({getItem:()=>'{'},'codex','codex'),null);
});
