const assert = require('node:assert/strict');
const {localName, open} = require('../../src/chemistry_agent/web/structure/import.js');
global.StructureParser = require('../../src/chemistry_agent/web/structure/parser.js');
global.StructureTrajectory = require('../../src/chemistry_agent/web/structure/trajectory-store.js');
global.StructureLammps = require('../../src/chemistry_agent/web/structure/lammps-metadata.js');
global.StructureMD = require('../../src/chemistry_agent/web/structure/md-stream.js');

(async () => {
  for (const name of ['POSCAR', 'CONTCAR', 'poscar.relaxed', 'sample.vasp', 'sample.poscar']) assert.equal(localName(name), 'POSCAR');
  for (const name of ['XDATCAR', 'XDATCAR.1', 'sample.xdatcar']) assert.equal(localName(name), 'XDATCAR');
  assert.equal(localName('sample.dump'), null);
  assert.equal(localName('unknown', 'gromacs'), null);
  assert.equal(localName('unknown', 'vasp'), 'POSCAR');
  const xyz = '1\n\nCu 1 2 3\n';
  const file = new File([xyz], 'sample.xyz');
  global.fetch = () => { throw Error('Local file must not be uploaded'); };
  const local = await open(file);
  assert.equal((await local.store.get(0)).frame.atoms[0].elem, 'Cu');
  let request;
  global.fetch = async (url, options) => {
    request = {url, options};
    return new Response(xyz, {headers:{'X-Structure-Format':'lammps-data'}});
  };
  const data = new File(['LAMMPS data\n1 atoms\n1 atom types\nMasses\n1 63.546 # Cu\nAtoms # atomic\n'], 'system.data');
  const imported = await open([data, new File(['units metal\nboundary p p f'], 'input.lammps')]);
  assert.equal(request.options.method, 'POST');
  assert.ok(request.url.includes('filename=system.data'));
  assert.ok(request.url.includes('type_map=Cu'));
  assert.ok(request.url.includes('atom_style=atomic'));
  assert.ok(imported.note.includes('自动识别 Cu'));
  assert.equal(request.options.body,data); // Companion inputs never uploaded/executed.
  assert.equal(imported.store.count, 1);
  global.fetch = async () => new Response('{}', {status:401});
  await assert.rejects(open(new File(['x'], 'x.pdb')), /登录已过期/);
  await assert.rejects(open([file,new File([xyz],'other.xyz')]),/一条轨迹/);
  await assert.rejects(open([file,new File(['units real'],'in.lammps')]),/联合读取/);
  const dump='ITEM: TIMESTEP\n0\nITEM: NUMBER OF ATOMS\n1\nITEM: BOX BOUNDS pp pp pp\n0 10\n0 10\n0 10\nITEM: ATOMS id type x y z\n1 1 1 2 3\n';
  global.fetch=()=>{throw Error('Dump and companion files must stay local');};
  const batch=await open([data,new File([dump],'trajectory.dump'),new File(['units metal'],'in.lammps')]);
  assert.equal(batch.name,'trajectory.dump');
  assert.equal((await batch.store.get(0)).frame.atoms[0].elem,'Cu');
  await assert.rejects(open(new File([dump],'trajectory.dump')),/无法识别/);
  console.log('Import: local routing, backend conversion, units notice and login expiry passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
