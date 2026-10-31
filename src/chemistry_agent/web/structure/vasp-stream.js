/* Progressive reads from the browser file picker, without File.text() on the whole trajectory. */
const VASPStream = (() => {
  async function* lines(file) {
    const reader=file.stream().getReader(),decoder=new TextDecoder();
    let pending='';
    try {
      while(true){
        const {done,value}=await reader.read();
        pending+=decoder.decode(value||new Uint8Array(),{stream:!done});
        let start=0,end;
        while((end=pending.indexOf('\n',start))>=0){yield pending.slice(start,end).replace(/\r$/,'');start=end+1;}
        pending=pending.slice(start);
        if(done){if(pending)yield pending.replace(/\r$/,'');break;}
      }
    } finally {await reader.cancel();reader.releaseLock();}
  }
  async function* frames(file) {
    const input=lines(file);
    const required=async()=>{const row=await input.next();if(row.done)throw Error('VASP 结构帧不完整');return row.value;};
    try {
      const header=[];for(let i=0;i<7;i++)header.push(await required());
      const counts=header[6].trim().split(/\s+/).map(Number),count=counts.reduce((a,b)=>a+b,0);
      if(!counts.length||counts.some(n=>!Number.isSafeInteger(n)||n<0)||count<1)throw Error('VASP 元素计数无效');
      let mode=await required();if(/^s/i.test(mode.trim())){header.push(mode);mode=await required();}
      const trajectory=/^XDATCAR$/i.test(file.name)||/configuration/i.test(mode);
      while(true){
        const rows=[...header,mode];for(let i=0;i<count;i++)rows.push(await required());
        yield rows.join('\n')+'\n';
        if(!trajectory)break;
        let next=await input.next();while(!next.done&&!next.value.trim())next=await input.next();
        if(next.done)break;mode=next.value;
      }
    } finally {await input.return();}
  }
  return {frames};
})();
if(typeof module!=='undefined'&&module.exports)module.exports=VASPStream;
