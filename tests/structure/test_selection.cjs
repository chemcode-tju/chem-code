const assert=require('node:assert/strict');
const {measure,remap,DisplayState,inRectangle}=require('../../src/chemistry_agent/web/structure/selection.js');
const atoms=[{elem:'H',id:'a',x:1,y:0,z:0},{elem:'O',id:'b',x:0,y:0,z:0},{elem:'C',id:'c',x:0,y:1,z:0},{elem:'H',id:'d',x:0,y:1,z:1},{elem:'N',id:'e',x:1,y:1,z:1}];
assert.match(measure(atoms,[0,1]),/1\.0000 Å/);
assert.match(measure(atoms,[0,1,2]),/90\.000°/);
assert.match(measure(atoms,[0,1,2,3]),/-90\.000°/);
assert.match(measure(atoms,[3,2,1,0]),/-90\.000°/);
assert.equal(measure(atoms,[0,1,2,3,4]),'');
assert.match(measure(atoms,[0,1,1]),/未定义/);
assert.match(measure([{x:0,y:0,z:0},{x:1,y:0,z:0},{x:2,y:0,z:0},{x:3,y:0,z:0}],[0,1,2,3]),/共线/);
const frame={atoms},reordered={atoms:[atoms[2],atoms[0],atoms[1],atoms[3],atoms[4]]};
assert.deepEqual(remap(frame,reordered,[0,1,2]),[1,2,0]);
const noId={atoms:atoms.map(({id,...a})=>a)};
assert.deepEqual(remap(noId,{atoms:[noId.atoms[1],noId.atoms[0]]},[0]),[]);
const state=new DisplayState(),original=JSON.stringify(frame);
state.apply(frame,[0],{style:'ballstick',palette:'custom',color:'#123456'});
assert.equal(state.resolve(atoms[0],0,{style:'sphere'}).style,'ballstick');
assert.equal(state.resolve(atoms[1],1,{style:'sphere'}).style,'sphere');
state.hiddenElements.add('H');assert.equal(state.resolve(atoms[0],0,{}).visible,false);
state.apply(frame,[0],{visible:true});assert.equal(state.resolve(atoms[0],0,{}).visible,false);
state.hiddenElements.clear();state.transition(frame,reordered);assert.equal(state.resolve(atoms[0],1,{}).color,'#123456');
state.reset(reordered,[1]);assert.equal(state.overrides.size,0);
assert.equal(JSON.stringify(frame),original);
assert.deepEqual(inRectangle([{x:5,y:5,index:3},{x:1,y:1,index:2},{x:12,y:9,index:0}],{x:10,y:10},{x:2,y:2}),[3]);
console.log('Selection: ordered distances/angles/dihedrals, degeneracy, stable IDs, hidden elements, immutable display rules, reverse box drag passed.');
const {outlineRing}=require('../../src/chemistry_agent/web/structure/selection.js');
const center={x:2,y:-3,z:5};
for(const normal of [[0,0,1],[1,0,0],[.3,.4,.5]])for(const distance of [Infinity,10]) {
  const mesh=outlineRing(center,1.28,normal,.03,distance),n=normal.map(v=>v/Math.hypot(...normal));
  const offset=Number.isFinite(distance)?1.28**2/distance:0;
  for(const p of mesh.vertexArr)assert.ok(Math.abs((p.x-center.x)*n[0]+(p.y-center.y)*n[1]+(p.z-center.z)*n[2]-offset)<1e-10);
  assert.equal(mesh.faceArr.length,48*6);assert.equal(mesh.color,0x004500);
  for(let j=0;j<mesh.faceArr.length;j+=3) {
    const [a,b,c]=mesh.faceArr.slice(j,j+3).map(i=>mesh.vertexArr[i]);
    const u=[b.x-a.x,b.y-a.y,b.z-a.z],v=[c.x-a.x,c.y-a.y,c.z-a.z];
    const cross=[u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0]];
    assert.ok(cross.reduce((sum,x,i)=>sum+x*n[i],0)>0);
  }
}
console.log('Selection silhouette: camera-facing tangent planes, perspective correction and outward faces passed.');

const {toggleIndices,formula}=require('../../src/chemistry_agent/web/structure/selection.js');
assert.deepEqual(toggleIndices([0,1,2],[1,2,3]),[0,3]);
assert.deepEqual(toggleIndices([0,3],[1,2,3]),[0,1,2]);
assert.equal(formula([{elem:'H'},{elem:'C'},{elem:'C'},{elem:'C'},{elem:'O'}],[0,1,2,3,4]),'H+3C+O');
