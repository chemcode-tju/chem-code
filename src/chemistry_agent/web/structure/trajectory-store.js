/* A growing byte index plus a bounded LRU of decoded frames. No all-frame text. */
(function(root) {
  'use strict';
  class IncompleteStructureFrame extends Error {}
  const pause=delay=>new Promise(resolve=>setTimeout(resolve,delay));
  class FrameStore {
    constructor(name,source,{maxFrames=16,maxBytes=64*1024*1024,readAhead=8}={}) {
      this.name=name;this.source=source;this.maxFrames=maxFrames;this.maxBytes=maxBytes;this.readAhead=readAhead;
      this.cache=new Map();this.pending=new Map();this.bytes=0;this.disposed=false;
      this.active=true;this.prefetchTask=null;
      this.vaspPreview=/^(POSCAR|CONTCAR|XDATCAR)$/i.test(name)||/\.vasp$/i.test(name);
      this.xyz=!this.vaspPreview;
    }
    get count(){return this.source.count;}
    get complete(){return this.source.complete;}
    decode(text) {
      return new StructureParser.StructureDocument(this.vaspPreview?'POSCAR':'preview.extxyz',text).frame(0);
    }
    put(index,text,decode=true) {
      const frame=decode ? this.decode(text) : null;
      // Conservative accounting, not a JavaScript heap measurement. One oversized
      // frame is allowed so the cache budget never becomes a file-size limit.
      const bytes=text.length*2+(frame?.atoms.length ?? (Number(text.slice(0,text.indexOf("\n")).trim()) || 0))*320+1024,record={frame,text,bytes};
      if(this.cache.has(index))this.bytes-=this.cache.get(index).bytes;
      this.cache.delete(index);this.cache.set(index,record);this.bytes+=bytes;
      while(this.cache.size>1&&(this.cache.size>this.maxFrames||this.bytes>this.maxBytes)) {
        const oldest=this.cache.keys().next().value;this.bytes-=this.cache.get(oldest).bytes;this.cache.delete(oldest);
      }
      return record;
    }
    async get(index) {
      if(this.disposed)throw Error('结构文件已关闭');
      if(!Number.isInteger(index)||index<0||index>=this.count)throw Error('帧尚未索引或超出范围');
      if(this.cache.has(index)) {
        const record=this.cache.get(index);
        this.cache.delete(index);
        this.cache.set(index,record);
        record.frame ??= this.decode(record.text);
        return record;
      }
      const prefetched=this.prefetchTargets?.get(index);
      if(prefetched) {
        await prefetched;
        if(this.disposed)throw Error('结构文件已关闭');
        if(this.cache.has(index))return this.get(index);
      }
      if(!this.pending.has(index)) {
        const task=this.source.read(index).then(text=>{if(this.disposed)throw Error('结构文件已关闭');return this.put(index,text);}).finally(()=>this.pending.delete(index));
        this.pending.set(index,task);
      }
      return this.pending.get(index);
    }
    async advance() {
      if(this.disposed || this.complete)return;
      if(!this.indexTask)this.indexTask=Promise.resolve().then(()=>this.source.advance())
        .finally(()=>{this.indexTask=null;});
      return this.indexTask;
    }
    async findIonicStep(step) {
      if(!Number.isSafeInteger(step) || step<1)throw Error('离子步编号无效');
      if(!Array.isArray(this.source.ionicSteps))throw Error('该结构文件没有离子步与轨迹帧的对应信息');
      // Complete the byte index, not the decoded frames. A concatenated restart
      // may contain an older copy of the same step; OSZICAR uses the latest run.
      while(!this.complete && !this.disposed)await this.advance();
      if(this.disposed)throw Error('结构文件已关闭');
      const start=this.source.segmentStart || 0;
      const index=this.source.ionicSteps.indexOf(step,start);
      if(index<0)throw Error(`轨迹中尚无第 ${step} 个离子步的结构`);
      return index;
    }
    async findSimulationStep(step,{nearest=false}={}) {
      if(!Number.isSafeInteger(step) || step<0)throw Error('MD 步编号无效');
      if(!Array.isArray(this.source.simulationSteps))throw Error('该文件没有 MD 步与轨迹帧的对应信息');
      while(!this.complete && !this.disposed)await this.advance();
      if(this.disposed)throw Error('结构文件已关闭');
      const steps=this.source.simulationSteps,start=this.source.segmentStart || 0;
      const exact=steps.indexOf(step,start);
      if(exact>=0)return exact;
      if(!nearest || step<steps[start] || step>steps.at(-1))throw Error(`轨迹中尚无 MD 步 ${step} 的结构`);
      let index=start;
      for(let i=start+1;i<steps.length;i++)if(Math.abs(steps[i]-step)<Math.abs(steps[index]-step))index=i;
      return index;
    }
    setActive(active){this.active=active;}
    // Atom Lens keeps a small window of raw neighbors and decodes only the
    // frame being displayed. An explicit jump outside that window can read immediately.
    prefetch(index,step) {
      if(!this.source.readMany || this.prefetchTask || this.pending.size || !this.active || this.disposed)return;
      const largest=Math.max(1024,...[...this.cache.values()].map(record=>record.bytes));
      const capacity=Math.min(this.readAhead,this.maxFrames-1,Math.floor(this.maxBytes/largest)-1);
      if(capacity<1)return;
      const targets=[];
      for(let i=index+step;i>=0&&i<this.count&&targets.length<capacity;i+=step)targets.push(i);
      const lowWatermark = Math.min(capacity, Math.ceil(capacity / 3));
      const missing=targets.findIndex(i=>!this.cache.has(i));
      if(missing<0 || missing>=lowWatermark)return;

      const wanted=targets.slice(missing);
      const ready=new Map();
      this.prefetchTargets=new Map(wanted.map(target=>[target,new Promise(resolve=>ready.set(target,resolve))]));
      this.prefetchTask=Promise.resolve().then(()=>this.source.readMany(wanted,(target,text)=>{
        if(!this.disposed && !this.cache.has(target))this.put(target,text,false);
        ready.get(target)?.();
      })).catch(()=>{/* Foreground reads report failures and may retry. */})
        .finally(()=>{
          for(const resolve of ready.values())resolve();
          this.prefetchTask=null;
          this.prefetchTargets=null;
        });
      return this.prefetchTask;
    }
    async scan(onProgress) {
      while(!this.disposed&&!this.complete) {
        await pause(this.source.scanDelay ?? 40);if(this.disposed)return;
        if(!this.active) {await pause(120);continue;}
        // Indexing extends the seekable timeline independent of the frame
        // buffer. HTTP index and frame reads can progress concurrently.
        await this.advance();if(!this.disposed)onProgress();
      }
    }
    dispose(){this.disposed=true;this.cache.clear();this.bytes=0;this.source.dispose?.();}
  }
  class HostSource {
    constructor(call,batch) {
      this.call=call;this.version=batch.version;this.offsets=[];this.offset=0;this.count=0;this.complete=false;
      this.accept(batch);
    }
    accept(batch) {
      if(!Array.isArray(batch.offsets)||!Number.isSafeInteger(batch.nextOffset)||batch.nextOffset<this.offset||(!batch.eof&&batch.nextOffset===this.offset))throw Error('分段读取返回无效数据');
      this.offsets.push(...batch.offsets);this.offset=batch.nextOffset;this.complete=batch.eof;
      this.binary=Number.isSafeInteger(batch.totalFrames);
      this.count=this.binary?batch.totalFrames:this.offsets.length;
      if(this.binary)this.complete=true;
    }
    async read(index){const batch=await this.call({offset:this.binary?index:this.offsets[index],frameLimit:1,version:this.version});return batch.text;}
    async advance(){this.accept(await this.call({offset:this.offset,frameLimit:4096,indexOnly:true,version:this.version}));}
  }
  // The scanner retains at most one 64 KiB chunk; offsets refer to UTF-8 bytes,
  // not JS characters, including CRLF and multibyte comments.
  class ByteLines {
    constructor(file){this.file=file;this.offset=0;this.buffer=new Uint8Array();this.cursor=0;this.decoder=new TextDecoder();}
    async next() {
      const start=this.offset,parts=[];
      while(true) {
        if(this.cursor===this.buffer.length) {
          if(this.offset>=this.file.size)break;
          this.buffer=new Uint8Array(await this.file.slice(this.offset,this.offset+65536).arrayBuffer());this.cursor=0;
        }
        const end=this.buffer.indexOf(10,this.cursor),stop=end<0?this.buffer.length:end+1;
        parts.push(this.decoder.decode(this.buffer.subarray(this.cursor,stop),{stream:true}));
        this.offset+=stop-this.cursor;this.cursor=stop;
        if(end>=0)break;
      }
      parts.push(this.decoder.decode());
      if(start===this.offset)return null;
      return {start,end:this.offset,text:parts.join('').replace(/\r?\n$/,'')};
    }
  }
  class LocalSource {
    constructor(file,name) {
      this.file=file;this.name=name;this.lines=new ByteLines(file);this.offsets=[];this.ends=[];this.count=0;this.complete=false;
      this.vasp=/^(POSCAR|CONTCAR|XDATCAR)$/i.test(name)||/\.vasp$/i.test(name);this.header='';this.frameHeaders=[];
      if(this.vasp){this.ionicSteps=[];this.segmentStart=0;}
    }
    async required(){const row=await this.lines.next();if(!row)throw new IncompleteStructureFrame('轨迹末尾存在不完整帧');return row;}
    async initialize() {
      if(/\.traj$/i.test(this.name)) {this.binary=await ASETrajectory.open(this.file);this.count=this.binary.count;this.complete=true;return;}
      if(this.vasp) await this.readHeader(await this.required());
      await this.advance(1);
    }
    async readHeader(first) {
        const rows=[first.text];for(let i=1;i<7;i++)rows.push((await this.required()).text);
        this.mode=await this.required();if(/^s/i.test(this.mode.text.trim())){rows.push(this.mode.text);this.mode=await this.required();}
        this.trajectory=/^XDATCAR$/i.test(this.name)||/configuration/i.test(this.mode.text);
      this.setHeader(rows);
    }
    setHeader(rows) {
      const counts=rows[6].trim().split(/\s+/).map(Number);this.atomCount=counts.reduce((a,b)=>a+b,0);
      if(counts.some(n=>!Number.isSafeInteger(n)||n<0)||this.atomCount<1)throw Error('VASP 元素计数无效');
        this.header=rows.join('\n')+'\n';
    }
    resumeVasp(offset, header) {
      this.lines.offset=offset;
      this.setHeader(header.replace(/\r?\n$/,'').split(/\r?\n/));
      this.trajectory=true;
      this.resumed=true;
    }
    async advance(limit=4096) {
      const initial=this.lines.offset;
      try {
      for(let i=0;i<limit&&!this.complete;i++) {
        let row=this.mode||await this.lines.next();this.mode=null;
        while(row&&!row.text.trim())row=await this.lines.next();
        if(!row){this.complete=true;break;}
        if(this.vasp && this.trajectory && (this.count || this.resumed) && !/^(direct|cartesian|kartesian|d|c|k)(?:\s+configuration\s*=.*)?$/i.test(row.text.trim())) {
          await this.readHeader(row);
          row=this.mode;this.mode=null;
        }
        const start=row.start;
        let count=this.atomCount;
        if(this.vasp) {if(!/^[dck]/i.test(row.text.trim()))throw Error('无法识别 VASP 坐标模式');}
        else {
          if(!/^\s*\d+\s*$/.test(row.text))throw Error('轨迹原子数无效');
          count=Number(row.text);if(!Number.isSafeInteger(count)||count<1||count>100000)throw Error('原子数必须在 1–100000 范围内');
          await this.required();
        }
        for(let j=0;j<count;j++)if(!(await this.required()).text.trim())throw Error('轨迹末尾存在不完整帧');
        if(this.vasp) {
          this.frameHeaders.push(this.header);
          const configuration=/configuration\s*=\s*(\d+)/i.exec(row.text);
          const step=configuration?Number(configuration[1]):null;
          const previous=this.ionicSteps.at(-1);
          if(step!==null && previous!==null && previous!==undefined && step<=previous)this.segmentStart=this.count;
          this.ionicSteps.push(step);
        }
        this.offsets.push(start);this.ends.push(this.lines.offset);this.count++;
        this.complete=this.lines.offset>=this.file.size||(this.vasp&&!this.trajectory);
        if(this.lines.offset-initial>=1048576)break;
      }
      } catch (error) {
        if (!(error instanceof IncompleteStructureFrame) || !this.file.frozen || !(this.count || this.resumed)) throw error;
        this.complete = true;
      }
    }
    async read(index) {
      if(this.binary)return this.binary.frameText(index);
      return (this.frameHeaders[index] || '')+await this.file.slice(this.offsets[index],this.ends[index]).text();
    }
    async readMany(indices,onFrame) {
      const start=this.offsets[indices[0]];
      const targets=indices.filter((index,order)=>!order || this.ends[index]-start<=8*1024*1024);
      // Begin nearest frames immediately while warming later aligned blocks.
      if(this.file.warm && targets.length>1 && targets[1]===targets[0]+1) {
        const warming=this.file.warm(start,this.ends[targets.at(-1)]).catch(()=>{});
        await Promise.all(targets.map(async index=>onFrame(index,await this.read(index))));
        await warming;
        return;
      }
      // For sparse seeks, read only the requested frame spans.
      for(let i=0;i<targets.length;i+=4) {
        await Promise.all(targets.slice(i,i+4).map(async index=>onFrame(index,await this.read(index))));
      }
    }
  }
  async function local(file,name=file.name,options) {
    const source=new LocalSource(file,name);
    await source.initialize();
    if(source.binary)source.readMany=undefined;
    const store=new FrameStore(name,source,{...file.playbackCache,...options});
    await store.get(0);
    return store;
  }
  function host(name,call,batch,options) {const store=new FrameStore(name,new HostSource(call,batch),options);store.put(0,batch.text);return store;}
  const api={IncompleteStructureFrame,FrameStore,HostSource,LocalSource,ByteLines,local,host};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;else root.StructureTrajectory=api;
})(typeof window!=='undefined'?window:globalThis);
