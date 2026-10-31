const assert=require('node:assert/strict');
const G=require('../../src/chemistry_agent/web/structure/geometry.js');
const {StructureDocument}=require('../../src/chemistry_agent/web/structure/parser.js');
const atom=(elem,x,y=0,z=0)=>({elem,x,y,z});
const frame={cell:[[10,0,0],[3,8,0],[1,2,9]],pbc:[true,true,false],atoms:[atom('O',-1,9,12)]};
const before=JSON.stringify(frame),wrapped=G.wrap(frame),fractional=G.basis(frame.cell).fractional([wrapped.atoms[0].x,wrapped.atoms[0].y,wrapped.atoms[0].z]);
assert.equal(JSON.stringify(frame),before);assert.ok(fractional[0]>=0&&fractional[0]<1&&fractional[1]>=0&&fractional[1]<1);assert.equal(wrapped.atoms[0].z,12);
assert.throws(()=>G.wrap({atoms:[]}),/晶胞/);
const water={cell:[[10,0,0],[0,10,0],[0,0,10]],pbc:[true,true,true],atoms:[atom('O',9),atom('H',9.98),atom('O',1.8)]};
assert.equal(G.hydrogenBonds(water).length,1);
assert.equal(G.hydrogenBonds({...water,pbc:[false,false,false]}).length,0);
assert.equal(G.hydrogenBonds({...water,atoms:[atom('O',0),atom('H',1),atom('O',1,2)]}).length,0);
// Check skew-cell MIC against brute force, including offsets far outside the cell.
const cell=[[8,0,0],[7.5,2,0],[1,1,8]],delta=[17,3,1];
let best=Infinity;for(let i=-5;i<=5;i++)for(let j=-5;j<=5;j++)for(let k=-3;k<=3;k++){const v=delta.map((x,d)=>x-i*cell[0][d]-j*cell[1][d]-k*cell[2][d]);best=Math.min(best,Math.hypot(...v));}
assert.ok(Math.abs(Math.hypot(...G.minimumImage(delta,cell,[true,true,true],4))-best)<1e-10);
const doc=new StructureDocument('id.extxyz','2\nProperties=species:S:1:pos:R:3:id:I:1\nO 0 0 0 9\nH 1 0 0 10\n');
const f=doc.frame(0);assert.equal(f.atoms[0].id,'9');
assert.equal(G.followAtom({...f,atoms:f.atoms.slice().reverse()},{id:'9',element:'O'}).elem,'O');
assert.throws(()=>G.followAtom(f,{index:1,element:'O',signature:'O,H'}),/无法对应/);
console.log('Periodic wrap, skewed MIC, hydrogen bond geometry, and ID following passed.');

const {removeDrift,centerOfMass}=require('../../src/chemistry_agent/web/structure/geometry.js');
const reference={atoms:[{elem:'H',mass:1,x:0,y:0,z:0},{elem:'O',mass:3,x:4,y:0,z:0}],cell:[[10,0,0],[0,10,0],[0,0,10]]};
const shifted={...reference,atoms:reference.atoms.map(a=>({...a,x:a.x+7,y:a.y-2,z:a.z+3}))};
const driftBefore=JSON.stringify(shifted),corrected=removeDrift(shifted,reference,[0,1],[0,1]);
assert.deepEqual(centerOfMass(reference.atoms,[0,1]),[3,0,0]);
assert.deepEqual(corrected.atoms,reference.atoms);assert.equal(corrected.cell,reference.cell);assert.equal(JSON.stringify(shifted),driftBefore);
const subset=removeDrift({...shifted,atoms:[shifted.atoms[0],{...shifted.atoms[1],x:20}]},reference,[0],[0]);
assert.equal(subset.atoms[1].x,13);assert.throws(()=>removeDrift(shifted,reference,[],[]));

const massDoc=new StructureDocument('mass.extxyz','1\nProperties=species:S:1:pos:R:3:mass:R:1\nH 0 0 0 2.014\n');assert.equal(massDoc.frame(0).atoms[0].mass,2.014);
