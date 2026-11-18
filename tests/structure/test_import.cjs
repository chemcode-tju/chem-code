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
  const cif = new File(['data_copper'], 'copper.CIF');
  const convertedCif = await open(cif);
  assert.equal(new URL(request.url, 'http://localhost').searchParams.get('format'), 'cif');
  assert.equal(request.options.body, cif);
  assert.equal((await convertedCif.store.get(0)).frame.atoms[0].elem, 'Cu');
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
  const otherData = new File(['LAMMPS data\n1 atoms\n1 atom types\nMasses\n1 15.999 # O\n'],'other.data');
  const run = new File(['units metal\nread_data "system.data"\ndump own all custom 10 trajectory.dump id type x y z\n'
    +'dump extra all custom 10 auxiliary.dump id type x y z\ndump_modify extra element O\n'],'in.target');
  const unrelated = new File(['units real\nread_data other.data\ndump own all custom 10 other.dump id type x y z\n'],'in.other');
  const trace = new File([dump],'trajectory.dump');
  const matched = await open([unrelated,otherData,run,trace,data]);
  assert.equal((await matched.store.get(0)).frame.atoms[0].elem,'Cu');
  assert.match(matched.note,/system\.data/);
  assert.doesNotMatch(matched.note,/other\.data/);
  await assert.rejects(open([trace,otherData,data]),/无法确定.*other\.data.*system\.data/);
  const cp2k = new File(['1\ni = 2, time = 1.0\nCu 1 2 3\n1\ni = 4, time = 2.0 pbc="T T F"\nCu 1 2 3\n'],'run-pos-1.xyz');
  const cell = new File(['# Step Time [fs] Ax [angstrom]\n0 0 3 0 0 0 3 0 0 0 3\n'
    +'4 2 7 0 0 1 8 0 0 0 9\n2 1 5 0 0 0 5 0 0 0 5\n'],'run-1.cell');
  const paired = await open([cell,cp2k]);
  assert.deepEqual((await paired.store.get(0)).frame.cell,[[5,0,0],[0,5,0],[0,0,5]]);
  await paired.store.advance();
  const second = (await paired.store.get(1)).frame;
  assert.deepEqual(second.cell,[[7,0,0],[1,8,0],[0,0,9]]);
  assert.deepEqual(second.pbc,[true,true,false]);
  await assert.rejects(open([cp2k,new File(['3 0 5 0 0 0 5 0 0 0 5'],'wrong.cell')]),/找不到 XYZ 步号 2/);
  console.log('Import: local routing, backend conversion, units notice and login expiry passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
