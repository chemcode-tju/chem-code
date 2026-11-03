const assert=require('node:assert/strict');
const C=require('../../src/chemistry_agent/web/structure/controls.js');
assert.deepEqual(C.atomIndices('1, 3, 5-8，3',10),[0,2,4,5,6,7]);
for(const value of ['', '0','2-1','11','1.5','1-10000000'])assert.throws(()=>C.atomIndices(value,10));
for(const count of [1,2,20,800,38143,1000000])for(const width of [30,80,150,300,700]){
 const measure=t=>t.length*7,marks=C.timelineTicks(count,width,measure);
 for(let i=0;i<marks.length;i++){
  const mark=marks[i],half=measure(String(mark.value))/2;
  if(width>=2*half){assert.ok(mark.x-half>=0);assert.ok(mark.x+half<=width);}
  if(i){const previous=marks[i-1];assert.ok(mark.x-half-(previous.x+measure(String(previous.value))/2)>=11.99);}
 }
}
const endpoints=C.timelineTicks(1000000,300,text=>text.length*7);
assert.equal(endpoints[0].tickX,6);
assert.equal(endpoints.at(-1).tickX,294);
assert.ok(endpoints.at(-1).x < endpoints.at(-1).tickX);
console.log('Atom range validation and nonoverlapping ticks at 30–700 px passed.');
