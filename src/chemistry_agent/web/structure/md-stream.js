/* Native, bounded-memory GRO and LAMMPS dump readers. Coordinates are converted only on demand. */
(function(root) {
  'use strict';
  const number=value=>{if(value===undefined||String(value).trim()===''||!Number.isFinite(Number(value)))throw Error(`无效数值：${value}`);return Number(value);};
  function count(value) {const n=number(value);if(!Number.isSafeInteger(n)||n<1||n>100000)throw Error('每帧原子数必须为 1–100000');return n;}
  function groElement(name) {
    const stripped=name.replace(/^\d+/, '');
    if(StructureLammps.element(stripped))return stripped;
    const two=stripped.slice(0,2);
    if(StructureLammps.element(two))return two;
    if(StructureLammps.element(stripped[0]))return stripped[0];
    throw Error(`无法从 GRO 原子名 ${name} 识别元素；虚拟位点/粗粒化原子不应当作真实元素`);
  }
  function xyz(atoms,cell,pbc,withIds=false,withMass=false) {
    const properties='species:S:1:pos:R:3'+(withIds?':id:I:1':'')+(withMass?':mass:R:1':'');
    const header=`Lattice="${cell.flat().join(' ')}" Properties=${properties} pbc="${pbc.map(v=>v?'T':'F').join(' ')}"`;
    return `${atoms.length}\n${header}\n`+atoms.map(a=>`${a.elem} ${a.position.join(' ')}${withIds?' '+a.id:''}${withMass?' '+a.mass:''}`).join('\n')+'\n';
  }
  function parseGro(text) {
    const lines=text.replace(/\r/g,'').trimEnd().split('\n'), n=count(lines[1]);
    if(lines.length!==n+3)throw Error('GRO 帧不完整或原子数与行数不一致');
    const atoms=lines.slice(2,n+2).map(line=>{
      // GRO precision is variable: consecutive decimal points determine field width.
      const decimals=[...line.slice(20).matchAll(/\./g)].map(match=>match.index);
      if(decimals.length<3)throw Error('GRO 坐标列不完整');
      const width=decimals[1]-decimals[0];if(width<5||decimals[2]-decimals[1]!==width)throw Error('GRO 坐标精度/列宽不一致');
      return {elem:groElement(line.slice(10,15).trim()),position:[0,1,2].map(i=>number(line.slice(20+i*width,20+(i+1)*width))*10)};
    });
    const box=lines[n+2].trim().split(/\s+/).map(number);
    if(![3,9].includes(box.length))throw Error('GRO 晶胞必须包含 3 或 9 个数值');
    const cell=[[box[0],box[3]||0,box[4]||0],[box[5]||0,box[1],box[6]||0],[box[7]||0,box[8]||0,box[2]]].map(row=>row.map(v=>v*10));
    const periodic=box.some(v=>v!==0);
    return xyz(atoms,cell,[periodic,periodic,periodic]);
  }
  function dumpHeader(text) {
    const lines=text.replace(/\r/g,'').trimEnd().split('\n');
    const stepIndex=lines.indexOf('ITEM: TIMESTEP');
    if(stepIndex<0)throw Error('不是 LAMMPS atom/custom dump');
    const timestep=number(lines[stepIndex+1]);
    const countIndex=lines.findIndex(line=>line==='ITEM: NUMBER OF ATOMS');
    const boxIndex=lines.findIndex(line=>line.startsWith('ITEM: BOX BOUNDS'));
    const atomIndex=lines.findIndex(line=>line.startsWith('ITEM: ATOMS '));
    if(countIndex<0||boxIndex<0||atomIndex<0)throw Error('LAMMPS dump 缺少 NUMBER OF ATOMS、BOX BOUNDS 或 ATOMS');
    const n=count(lines[countIndex+1]), columns=lines[atomIndex].trim().split(/\s+/).slice(2);
    if(lines.length!==atomIndex+1+n)throw Error('LAMMPS dump 原子行数与声明不一致');
    const rows=lines.slice(atomIndex+1).map(line=>line.trim().split(/\s+/));
    if(rows.some(row=>row.length!==columns.length))throw Error('LAMMPS dump 原子列数不一致');
    return {lines,timestep,n,columns,rows,boxIndex};
  }
  function dumpMetadata(text) {
    const data=dumpHeader(text), types=new Set(), mappings=new Map(), masses=new Map();
    const {columns,rows}=data, typeIndex=columns.indexOf('type'), elementIndex=columns.indexOf('element'), labelIndex=columns.indexOf('typelabel'),massIndex=columns.indexOf('mass');
    for(const row of rows) {
      const type=typeIndex>=0?row[typeIndex]:labelIndex>=0?row[labelIndex]:null;
      if(type!==null)types.add(type);
      const symbol=elementIndex>=0?row[elementIndex]:labelIndex>=0?row[labelIndex]:type;
      if(type!==null&&StructureLammps.element(symbol)) {
        if(mappings.has(type)&&mappings.get(type)!==symbol)throw Error(`dump 中 type ${type} 有互相冲突的元素`);
        mappings.set(type,symbol);
      }
      if(type!==null&&massIndex>=0){const mass=number(row[massIndex]);if(masses.has(type)&&Math.abs(masses.get(type)-mass)>0.03)throw Error(`type ${type} 有多种质量，无法用单一类型映射`);masses.set(type,mass);}
    }
    let metadata='';
    for(const [type,mass] of masses)metadata+=`mass ${type} ${mass}\n`;
    const unitsIndex=data.lines.indexOf('ITEM: UNITS');
    if(unitsIndex>=0)metadata+=`units ${data.lines[unitsIndex+1]}\n`;
    return {types:[...types],text:metadata,elementMap:Object.fromEntries(mappings),timestep:data.timestep};
  }
  function parseDump(text, metadata) {
    const {lines,columns,rows,boxIndex}=dumpHeader(text);
    const unitsIndex=lines.indexOf('ITEM: UNITS');
    if(unitsIndex>=0 && lines[unitsIndex+1]!==metadata.units)throw Error('轨迹内 units 与配套文件不一致');
    const boxHeader=lines[boxIndex].trim().split(/\s+/).slice(3);
    const bounds=lines.slice(boxIndex+1,boxIndex+4).map(line=>line.trim().split(/\s+/).map(number));
    let cell,origin;
    if(boxHeader.includes('abc')&&boxHeader.includes('origin')) {
      if(bounds.some(row=>row.length!==4))throw Error('general triclinic 晶胞应为三行向量和原点分量');
      cell=bounds.map(row=>row.slice(0,3));origin=bounds.map(row=>row[3]);
    } else if(boxHeader.includes('xy')) {
      if(!['xy','xz','yz'].every(value=>boxHeader.includes(value))||bounds.some(row=>row.length!==3))throw Error('倾斜晶胞定义不完整');
      const [xy,xz,yz]=bounds.map(row=>row[2]);
      const xlo=bounds[0][0]-Math.min(0,xy,xz,xy+xz),xhi=bounds[0][1]-Math.max(0,xy,xz,xy+xz);
      const ylo=bounds[1][0]-Math.min(0,yz),yhi=bounds[1][1]-Math.max(0,yz);
      origin=[xlo,ylo,bounds[2][0]];cell=[[xhi-xlo,0,0],[xy,yhi-ylo,0],[xz,yz,bounds[2][1]-bounds[2][0]]];
    } else {
      if(bounds.some(row=>row.length!==2))throw Error('正交晶胞列数无效');
      origin=bounds.map(row=>row[0]);cell=bounds.map((row,i)=>[0,1,2].map(j=>i===j?row[1]-row[0]:0));
    }
    const boundaries=boxHeader.filter(value=>/^[pfsm]{2}$/.test(value));
    const pbc=boundaries.length===3?boundaries.map(value=>value==='pp'):metadata.pbc||[false,false,false];
    const candidates=[['x','y','z'],['xu','yu','zu'],['xs','ys','zs'],['xsu','ysu','zsu']];
    const fields=candidates.find(values=>values.every(value=>columns.includes(value)));
    if(!fields)throw Error('dump 缺少支持的坐标列 x/y/z、xu/yu/zu、xs/ys/zs 或 xsu/ysu/zsu');
    const scaled=fields[0]==='xs'||fields[0]==='xsu',scale=metadata.lengthScale||1;
    const hasIds=columns.includes('id'), seen=new Set();
    const atoms=rows.map((row,index)=>{
      const field=name=>row[columns.indexOf(name)];
      const type=field('type')||field('typelabel');
      const explicit=field('element'),label=field('typelabel');
      const symbol=StructureLammps.element(explicit)||StructureLammps.element(label)||metadata.mapping[type]||StructureLammps.element(type);
      if(!symbol)throw Error(`第 ${index+1} 个原子的类型 ${type||explicit||label} 未能识别，请补充同次计算的 data/输入文件`);
      if(explicit&&StructureLammps.element(explicit)&&metadata.mapping[type]&&metadata.mapping[type]!==explicit)throw Error(`dump 元素与配套文件的 type ${type} 映射冲突`);
      if(label&&StructureLammps.element(label)&&metadata.mapping[type]&&metadata.mapping[type]!==label)throw Error(`dump 类型标签与配套文件的 type ${type} 映射冲突`);
      const values=fields.map(name=>number(field(name)));
      const position=(scaled?[0,1,2].map(j=>values.reduce((sum,value,i)=>sum+value*cell[i][j],0)):values.map((v,i)=>v-origin[i])).map(v=>v*scale);
      const id=hasIds?number(field('id')):index+1;
      if(hasIds&&(!Number.isSafeInteger(id)||id<=0||seen.has(id)))throw Error('原子 ID 必须是唯一的正安全整数');seen.add(id);
      const mass=columns.includes('mass')?number(field('mass')):metadata.masses[type]||StructureLammps.weights[symbol];
      return {elem:symbol,position,id,mass};
    });
    if(hasIds)atoms.sort((a,b)=>a.id-b.id);
    return xyz(atoms,cell.map(row=>row.map(v=>v*scale)),pbc,hasIds,atoms.every(a=>Number.isFinite(a.mass)&&a.mass>0));
  }
  class MDSource {
    constructor(file,format,metadata={}) {this.file=file;this.format=format;this.metadata=metadata;this.lines=new StructureTrajectory.ByteLines(file);this.offsets=[];this.ends=[];this.count=0;this.complete=false;if(format==='lammps-dump-text'){this.simulationSteps=[];this.segmentStart=0;}}
    async required(){const row=await this.lines.next();if(!row)throw Error('轨迹末尾存在不完整帧');return row;}
    async advance(limit=4096) {
      const initial=this.lines.offset;
      for(let i=0;i<limit&&!this.complete;i++) {
        let row=await this.lines.next();
        // Trailing blank lines are tolerated; every nonempty frame must be complete.
        while(row&&!row.text.trim())row=await this.lines.next();
        if(!row){this.complete=true;break;}
        const start=row.start;
        let n,simulationStep;
        if(this.format==='gromacs') {
          n=count((await this.required()).text);
          for(let j=0;j<n;j++)await this.required();
          await this.required();
        } else {
          while(row.text==='ITEM: TIME'||row.text==='ITEM: UNITS'){await this.required();row=await this.required();}
          if(row.text!=='ITEM: TIMESTEP')throw Error('仅支持 LAMMPS atom/custom 文本 dump');
          simulationStep=number((await this.required()).text);
          if(!Number.isSafeInteger(simulationStep) || simulationStep<0)throw Error('MD 步编号无效');
          row=await this.required();
          while(row.text==='ITEM: TIME'||row.text==='ITEM: UNITS'){await this.required();row=await this.required();}
          if(row.text!=='ITEM: NUMBER OF ATOMS')throw Error('dump 缺少 NUMBER OF ATOMS');
          n=count((await this.required()).text);
          if(!(await this.required()).text.startsWith('ITEM: BOX BOUNDS'))throw Error('dump 缺少 BOX BOUNDS');
          for(let j=0;j<3;j++)await this.required();
          if(!(await this.required()).text.startsWith('ITEM: ATOMS '))throw Error('dump 缺少 ATOMS 列');
          for(let j=0;j<n;j++)await this.required();
        }
        if(this.simulationSteps) {
          if(this.simulationSteps.length && simulationStep<=this.simulationSteps.at(-1))this.segmentStart=this.count;
          this.simulationSteps.push(simulationStep);
        }
        this.offsets.push(start);this.ends.push(this.lines.offset);this.count++;
        this.complete=this.lines.offset>=this.file.size;
        if(this.lines.offset-initial>=1048576)break;
      }
    }
    async raw(index){return this.file.slice(this.offsets[index],this.ends[index]).text();}
    async read(index){const text=await this.raw(index);return this.format==='gromacs'?parseGro(text):parseDump(text,this.metadata);}
  }
  async function open(file,format,companions=[]) {
    const source=new MDSource(file,format);await source.advance(1);
    if(!source.count)throw Error('没有结构帧');
    let note='GRO 元素按原子名识别，nm 已转为 Å';
    if(format==='lammps-dump-text') {
      const first=dumpMetadata(await source.raw(0));
      source.metadata=StructureLammps.resolve([...companions,{name:file.name,text:first.text,elementMap:first.elementMap}],first.types);
      const mappings=[...new Set(Object.entries(source.metadata.mapping).filter(([type])=>/^\d+$/.test(type)).map(([type,value])=>`${type}=${value}`))];
      note=`自动识别 ${mappings.join('、')||'dump 内的元素列'}${source.metadata.warnings.length?' · '+source.metadata.warnings.join('；'):''}`;
    }
    const store=new StructureTrajectory.FrameStore('preview.extxyz',source);await store.get(0);
    return {store,note:`本地逐帧读取 · 未上传文件 · ${note}`};
  }
  const api={MDSource,parseGro,parseDump,dumpMetadata,open};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;else root.StructureMD=api;
})(typeof window!=='undefined'?window:globalThis);
