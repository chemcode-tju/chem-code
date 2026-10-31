const assert=require('node:assert/strict');
const ASE=require('../../src/chemistry_agent/web/structure/ase-traj.js');
function int64(n){const b=Buffer.alloc(8);b.writeBigInt64LE(BigInt(n));return b;}
async function main(){
  const header=Buffer.concat([Buffer.from('- of UlmASE-Trajectory  '),int64(3),int64(2),int64(48)]);
  const numbers=int64(8),positions=Buffer.alloc(24);[1,2,3].forEach((v,i)=>positions.writeDoubleLE(v,i*8));
  const metadata={"numbers.":{ndarray:[[1],'int64',64]},"positions.":{ndarray:[[1,3],'float64',72]},cell:[[8,0,0],[2,9,0],[0,0,10]],pbc:[true,false,true]};
  metadata['masses.']={ndarray:[[1],'float64',96]};
  const masses=Buffer.alloc(8);masses.writeDoubleLE(17);
  const first=Buffer.from(JSON.stringify(metadata));delete metadata['numbers.'];delete metadata.pbc;
  const second=Buffer.from(JSON.stringify(metadata)),offset=104;
  const binary=Buffer.concat([header,int64(offset),int64(offset+8+first.length),numbers,positions,masses,int64(first.length),first,int64(second.length),second]);
  const trajectory=await ASE.open(new Blob([binary]));assert.equal(trajectory.count,2);
  assert.equal(await trajectory.frameText(0),await trajectory.frameText(1));assert.ok((await trajectory.frameText(1)).includes('mass:R:1'));assert.ok((await trajectory.frameText(1)).includes('O 1 2 3 17'));assert.ok((await trajectory.frameText(1)).includes('pbc="T F T"\nO 1 2 3'));
  await assert.rejects(()=>ASE.open(new Blob([Buffer.alloc(48)])),/ASE/);
  const truncated=await ASE.open(new Blob([binary.subarray(0,binary.length-1)]));await assert.rejects(()=>truncated.frameText(1),/不完整/);
  console.log('ASE browser reader: header inheritance, cell/PBC, exact coordinates and truncated-file rejection passed.');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
