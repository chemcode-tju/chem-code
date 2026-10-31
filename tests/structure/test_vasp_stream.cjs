const assert=require('node:assert/strict');
const {ProgressiveVASPDocument,StructureDocument}=require('../../src/chemistry_agent/web/structure/parser.js');
const VASP=require('../../src/chemistry_agent/web/structure/vasp-stream.js');
const header='Cu O\n2\n5 0 0\n0 5 0\n0 0 10\nCu O\n1 1\n';
async function main(){
 const frame='Direct configuration= 1\n0 0 0\n.5 .5 .25\n';
 const doc=new ProgressiveVASPDocument('XDATCAR');doc.append(header+frame);doc.append(header+frame.repeat(2));
 assert.equal(doc.count,3);assert.deepEqual(doc.frame(2).atoms[1],{elem:'O',x:5,y:5,z:5,serial:2});
 assert.deepEqual(new StructureDocument('POSCAR',doc.frameText(2)).frame(0).atoms,doc.frame(2).atoms);
 const file=new Blob([(header+frame.repeat(3)).replaceAll('\n','\r\n')]);file.name='XDATCAR';
 const result=new ProgressiveVASPDocument(file.name);for await(const text of VASP.frames(file))result.append(text);
 assert.equal(result.count,3);assert.deepEqual(result.frame(2),doc.frame(2));
 // Do not demand the rest of the file before yielding frame 1.
 const late=new Blob([header+frame+'incomplete\n']);late.name='XDATCAR';
 const iterator=VASP.frames(late);assert.equal((await iterator.next()).value,header+frame);await assert.rejects(iterator.next(),/不完整/);
 const poscar=new Blob([header+'Selective dynamics\nCartesian\n0 0 0 T T F\n1 2 3 F F F\n\nignored velocities\n']);poscar.name='CONTCAR';
 const single=new ProgressiveVASPDocument('CONTCAR');for await(const text of VASP.frames(poscar))single.append(text);
 assert.equal(single.count,1);assert.equal(single.frame(0).atoms[1].z,6);
 // UTF-8 and coordinate lines may cross arbitrary stream chunk boundaries.
 const raw=new TextEncoder().encode('铜 氧\n'+(header+frame).split('\n').slice(1).join('\n'));
 const split={name:'XDATCAR',stream(){let offset=0;return new ReadableStream({pull(controller){if(offset>=raw.length){controller.close();return;}controller.enqueue(raw.slice(offset,offset+2));offset+=2;}});}};
 const chunks=[];for await(const text of VASP.frames(split))chunks.push(text);
 assert.ok(chunks[0].startsWith('铜 氧\n'));assert.equal(chunks.length,1);
 console.log('Progressive VASP: physical scale, frame source, CRLF, selective Cartesian, early first frame, split UTF-8 passed.');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
