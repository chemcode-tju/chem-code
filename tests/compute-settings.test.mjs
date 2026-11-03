import assert from 'node:assert/strict';
import test from 'node:test';
import {serverPayload} from '../src/chemistry_agent/web/compute-ssh.mjs';
import {targetPayload} from '../src/chemistry_agent/web/compute-settings.mjs';

function form(data) {
  return {elements:{namedItem(name) {return {value:String(data[name] ?? ''),checked:data[name]===true};}}};
}

test('each SSH method sends only its relevant fields and no credentials in the connection request',()=>{
  const common={label:'lab',hostname:'cluster',scheduler:'pbspro',workdir:'/scratch/test'};
  const input={...common,username:'chemist',port:'2222',identity_file:'~/.ssh/test',password:'test-only'};
  assert.deepEqual(serverPayload(form({...input,auth_mode:'alias'})),{...common,auth_mode:'alias'});
  const direct={...common,auth_mode:'password',username:'chemist',port:2222};
  assert.deepEqual(serverPayload(form({...input,auth_mode:'password'})),direct);
  assert.deepEqual(serverPayload(form({...input,auth_mode:'key',identity_file:''})),{...direct,auth_mode:'key'});
  for(const identity_file of ['ssh-ed25519 public-data','~/.ssh/test.pub','/tmp/key','~/.ssh/../../key','-----BEGIN OPENSSH PRIVATE KEY-----'])
    assert.throws(()=>serverPayload(form({...input,auth_mode:'key',identity_file})),/私钥/);
  assert.throws(()=>serverPayload(form({...input,auth_mode:'key',port:'66000'})),/端口/);
});

test('environment resources belong to one server and preserve scheduler units and fallback policy',()=>{
  const software=[{id:'soft',server_id:'aa',name:'VASP'}],potentials=[{id:'p',server_id:'aa'}];
  const input={id:'day',code_id:'soft',potential_source_id:'p',queue:'batch',fallback:'low',minutes:90.5,nodes:2,
    ranks_per_node:16,memory_mb:8192,gpus_per_node:0,gpu_type:'stale',account:'materials',default:true};
  const env=targetPayload(form(input),'aa',software,potentials);
  assert.deepEqual(env.resources,{queue:'batch',wallclock_seconds:5430,nodes:2,ranks_per_node:16,
    memory_mb:8192,gpus_per_node:0,gpu_type:null,account:'materials'});
  assert.deepEqual(env.quota_fallback,{source:'batch',target:'low'});assert.equal(env.default,true);
  assert.throws(()=>targetPayload(form(input),'bb',software,potentials),/当前服务器/);
  assert.throws(()=>targetPayload(form(input),'aa',[{id:'soft',server_id:'aa',name:'VASP-tools',plugin:null}],potentials),/当前服务器/);
  assert.throws(()=>targetPayload(form({...input,fallback:'batch'}),'aa',software,potentials),/备用队列/);
  assert.throws(()=>targetPayload(form({...input,nodes:1.5}),'aa',software,potentials),/范围/);
});
