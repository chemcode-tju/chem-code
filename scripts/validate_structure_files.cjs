/* Compare actual browser parsers with independent ASE reference coordinates. */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

// Configuration
const corpusDirectory = 'data/cluster-b-structures';

global.StructureParser = require('../src/chemistry_agent/web/structure/parser.js');
global.ASETrajectory = require('../src/chemistry_agent/web/structure/ase-traj.js');
global.StructureTrajectory = require('../src/chemistry_agent/web/structure/trajectory-store.js');
global.StructureLammps = require('../src/chemistry_agent/web/structure/lammps-metadata.js');
const md = require('../src/chemistry_agent/web/structure/md-stream.js');
const {local} = StructureTrajectory;
const directory = path.resolve(__dirname, '..', corpusDirectory, 'validation');

function maximumError(actual, expected) {
  assert.equal(actual.length, expected.length);
  return actual.reduce((maximum, value, index) => Math.max(maximum, Math.abs(value - expected[index])), 0);
}

async function main() {
  const {cases} = JSON.parse(fs.readFileSync(path.join(directory, 'reference.json'), 'utf8'));
  const results = [];
  for (const entry of cases) {
    const blob=await fs.openAsBlob(entry.path);
    const companions=(entry.companions||[]).map(filename=>({name:path.basename(filename),text:fs.readFileSync(filename,'utf8')}));
    const store=['gromacs','lammps-dump-text'].includes(entry.format)
      ? (await md.open(blob,entry.format,companions)).store
      : await local(blob,entry.viewer_name);
    await store.scan(() => {});
    assert.equal(store.count, entry.count, `${entry.source}: frame count`);
    let coordinateError = 0, cellError = 0;
    for (const [index, expected] of Object.entries(entry.samples)) {
      const {frame} = await store.get(Number(index));
      assert.deepEqual(frame.atoms.map(atom => atom.elem), expected.symbols, `${entry.source}: elements`);
      coordinateError = Math.max(coordinateError, maximumError(
        frame.atoms.flatMap(atom => [atom.x, atom.y, atom.z]), expected.positions.flat()));
      const cell = frame.cell || [[0,0,0],[0,0,0],[0,0,0]];
      cellError = Math.max(cellError, maximumError(cell.flat(), expected.cell.flat()));
      assert.deepEqual(frame.pbc, expected.pbc, `${entry.source}: PBC`);
    }
    assert.ok(coordinateError < 1e-7, `${entry.source}: coordinate error ${coordinateError} Å`);
    assert.ok(cellError < 1e-7, `${entry.source}: cell error ${cellError} Å`);
    assert.ok(store.cache.size <= 16, 'Frame cache must remain bounded');
    const result = {source: entry.source, format: entry.format, frames: entry.count,
      atoms_first: entry.atoms_first, sampled_frames: Object.keys(entry.samples).map(Number),
      coordinate_error_angstrom: coordinateError, cell_error_angstrom: cellError, status: 'passed'};
    results.push(result);
    console.log(`Browser parser passed: ${entry.viewer_name}, ${entry.count} frames, ${entry.atoms_first} atoms, max error ${coordinateError} Å`);
    store.dispose();
  }
  fs.writeFileSync(path.join(directory, 'report.json'), JSON.stringify({status: 'passed', generated_at: new Date().toISOString(), results}, null, 2));
}
main().catch(error => {console.error(error); process.exitCode = 1;});
