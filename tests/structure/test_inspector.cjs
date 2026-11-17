'use strict';
const assert=require('node:assert/strict');
global.StructureParser=require('../../src/chemistry_agent/web/structure/parser.js');
global.StructureLammps=require('../../src/chemistry_agent/web/structure/lammps-metadata.js');
const Parser=StructureParser,Inspector=require('../../src/chemistry_agent/web/structure/inspector.js');
const frame=new Parser.StructureDocument('types.extxyz',Parser.serializeFrame([
 {elem:'C',type:'1',typeName:'sp2',id:17,mass:12.011,position:[1,2,3]},
 {elem:'C',type:'2',typeName:'sp3',id:21,mass:13,position:[4,5,6]},
 {elem:'H',name:'H "quoted"',residue:'A B',resid:'1',charge:.2,position:[7,8,9]}
],[[10,0,0],[-2,8,0],[1,2,6]],[true,false,true],{cell_origin:[-3,4,5]})).frame(0);
assert.equal(frame.atoms[0].type,'1');assert.equal(frame.atoms[1].typeName,'sp3');
assert.equal(frame.atoms[2].name,'H "quoted"');assert.equal(frame.atoms[2].residue,'A B');
assert.equal(frame.atoms[1].mass,13);assert.equal(frame.atoms[2].mass,undefined);assert.equal(frame.atoms[2].charge,.2);
// Missing fields are not invented, and two carbon force-field types stay separate.
const types=Inspector.types(frame.atoms);assert.equal(types.length,3);assert(types.some(t=>t.type==='—'));
assert.equal(types.filter(t=>t.elements.has('C')).length,2);
assert.deepEqual(Inspector.particles(frame,'sp3'),[1]);
assert.deepEqual(Inspector.particles(frame,'',new Set([2])),[2]);
const cell=Inspector.cellData(frame);assert.equal(cell.volume,480);assert.deepEqual(cell.bounds,[13,10,6]);
assert.deepEqual(cell.origin,[-3,4,5]);assert.deepEqual(cell.pbc,[true,false,true]);
assert(Math.abs(cell.lengths[1]-Math.sqrt(68))<1e-10);assert(cell.angles[2]>90);
assert.equal(Inspector.cellData({cell:null}),null);assert.equal(Inspector.cellData({cell:[[0,0,0],[0,0,0],[0,0,0]]}),null);
const large=Array.from({length:158559},(_,i)=>({elem:i%3?'H':'O',type:i%3?'HW':'OW',mass:i%3?1.008:15.999}));
assert.deepEqual(Inspector.types(large).map(t=>t.count),[105706,52853]);
console.log('Inspector: source types, escaped metadata, filtering, large-system counts, triclinic geometry and missing cells passed.');
