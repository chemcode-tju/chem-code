const assert = require('node:assert/strict');
const {localName, open} = require('../../src/chemistry_agent/web/structure/import.js');
global.StructureParser = require('../../src/chemistry_agent/web/structure/parser.js');
global.StructureTrajectory = require('../../src/chemistry_agent/web/structure/trajectory-store.js');

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
    return new Response(xyz, {headers:{'X-Structure-Format':'gromacs'}});
  };
  const imported = await open(new File(['fake gro'], 'water.gro'));
  assert.equal(request.options.method, 'POST');
  assert.ok(request.url.includes('filename=water.gro'));
  assert.ok(imported.note.includes('nm 已转为 Å'));
  assert.equal(imported.store.count, 1);
  global.fetch = async () => new Response('{}', {status:401});
  await assert.rejects(open(new File(['x'], 'x.gro')), /登录已过期/);
  console.log('Import: local routing, backend conversion, units notice and login expiry passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
