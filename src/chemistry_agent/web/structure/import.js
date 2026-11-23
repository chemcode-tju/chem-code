/* Browser file routing. Companion files are metadata only, never executable inputs. */
(function(root) {
  'use strict';
  function localName(name, format = 'auto') {
    if (format === 'extxyz') return 'preview.extxyz';
    if (format === 'vasp') return 'POSCAR';
    if (format === 'vasp-xdatcar') return 'XDATCAR';
    if (format !== 'auto') return null;
    if (/^XDATCAR(?:[._-].*)?$|\.xdatcar$/i.test(name)) return 'XDATCAR';
    if (/^(POSCAR|CONTCAR)(?:[._-].*)?$|\.(vasp|poscar|contcar)$/i.test(name)) return 'POSCAR';
    if (/\.(xyz|extxyz|traj)$/i.test(name)) return name;
    return null;
  }
  async function classify(file) {
    if (/\.cell$/i.test(file.name)) return 'cell';
    if (file.openIndexed) return 'local';
    const prefix=await file.slice(0,8192).text();
    if (/^ITEM: (?:TIMESTEP|UNITS|TIME)/m.test(prefix)) return 'lammps-dump-text';
    if (/^\s*\d+\s+atoms\s*$/m.test(prefix) && /^\s*\d+\s+atom types\s*$/m.test(prefix)) return 'lammps-data';
    if (localName(file.name)) return 'local';
    if (/\.gro$/i.test(file.name)) return 'gromacs';
    if (/\.pdb$/i.test(file.name)) return 'proteindatabank';
    if (/\.cif$/i.test(file.name)) return 'cif';
    return 'companion';
  }
  async function companionsText(files) {
    const limit = 1024 * 1024;
    return Promise.all(files.map(async file=>{
      const name=file.webkitRelativePath || file.name;
      if (file.size<=limit) return {name,text:await file.text()};
      const log=/^log(?:\.|$)|\.log$/i.test(file.name);
      const head = await file.slice(0,log ? limit/2 : limit).text();
      let text = head.slice(0,head.lastIndexOf('\n')+1);
      if (log) {
        const tail = await file.slice(file.size-limit/2,file.size).text();
        text += tail.slice(tail.indexOf('\n')+1,tail.lastIndexOf('\n')+1);
      }
      return {name,text};
    }));
  }
  async function openCp2k(file, cell) {
    const steps = new Map();
    let scale = 1;
    for (const row of (await cell.text()).split(/\r?\n/)) {
      const line = row.trim();
      if (!line) continue;
      if (line.startsWith('#')) {
        const unit = line.match(/\bAx\s*\[([^\]]+)\]/i)?.[1]?.trim().toLowerCase();
        if (!unit) continue;
        if (!['angstrom','å','bohr','a.u.'].includes(unit)) throw Error(`不支持的 CELL 长度单位：${unit}`);
        scale = ['bohr','a.u.'].includes(unit) ? 0.529177210903 : 1;
        continue;
      }
      const [step,time,...values] = line.split(/\s+/).map(value=>Number(value.replace(/[dD]/,'e')));
      const lattice = values.slice(0,9);
      if (!Number.isSafeInteger(step) || !Number.isFinite(time) || lattice.length!==9 || !lattice.every(Number.isFinite)) {
        throw Error(`CELL 行格式无效：${cell.name}`);
      }
      if (steps.has(step)) throw Error(`CELL 中的步号 ${step} 重复：${cell.name}`);
      steps.set(step,lattice.map(value=>value*scale));
    }
    const source = new StructureTrajectory.LocalSource(file,file.name);
    await source.initialize();
    const read = source.read.bind(source);
    source.read = async index => {
      const raw = await read(index);
      const firstLine = raw.indexOf('\n');
      const headerEnd = raw.indexOf('\n',firstLine+1);
      const header = raw.slice(firstLine+1,headerEnd).trim();
      const step = header.match(/(?:^|[,\s])i\s*=\s*(-?\d+)\b/i)?.[1];
      if (step === undefined) throw Error(`XYZ 第 ${index+1} 帧缺少 CP2K 步号 i`);
      const lattice = steps.get(Number(step));
      if (!lattice) throw Error(`CELL 中找不到 XYZ 步号 ${step}`);
      const clean = header.replace(/\bLattice="[^"]*"/i,'').trim();
      const pbc = /\bpbc\s*=/i.test(clean) ? '' : 'pbc="T T T" ';
      return `${raw.slice(0,firstLine+1)}Lattice="${lattice.join(' ')}" ${pbc}${clean}${raw.slice(headerEnd)}`;
    };
    const store = new StructureTrajectory.FrameStore(file.name,source);
    try { await store.get(0); } catch (error) { store.dispose(); throw error; }
    return {name:file.name,store,note:`CP2K XYZ + CELL · ${cell.name} · XYZ 未注明 PBC 时按三维周期显示`};
  }
  async function openSequence(files, name) {
    if(!files.length)throw Error('路径尚无结构像');
    // Explicit sequence order only; never infer a trajectory from companion files.
    let signature=null;
    const source={count:files.length,complete:true,async read(index) {
      const text=await files[index].text();
      const parsed=new StructureParser.StructureDocument('POSCAR',text);
      const elements=parsed.frame(0).atoms.map(atom=>atom.elem).join(',');
      if(signature!==null && signature!==elements)throw Error('路径像的原子数或元素顺序不一致');
      signature=elements;return text;
    }};
    const store=new StructureTrajectory.FrameStore('POSCAR',source);
    try {await store.get(0);}catch(error){store.dispose();throw error;}
    return {name,store,note:`NEB 路径 · ${files.length} 个实际像 · 非时间轨迹`};
  }
  async function open(input, {format = 'auto',sequence=false,name:sequenceName='NEB 路径',companions:remoteCompanions} = {}) {
    const files=input && typeof input.slice==='function' && typeof input.name==='string' ? [input] : Array.from(input||[]);
    if(sequence)return openSequence(files,sequenceName);
    if(!files.length || files.length>32)throw Error('请选择 1–32 个结构及配套文件');
    if (files.length === 1 && files[0].openIndexed) {
      return {name:files[0].name, store:await files[0].openIndexed(), note:''};
    }
    const entries=await Promise.all(files.map(async entry=>{
      const file = entry.initialize ? await entry.initialize() : entry;
      return {file,format:await classify(file)};
    }));
    if(files.length===1 && format!=='auto')entries[0].format=format;
    const dumps=entries.filter(entry=>entry.format==='lammps-dump-text');
    const cells = entries.filter(entry=>entry.format==='cell');
    const candidates=entries.filter(entry=>!['companion','cell'].includes(entry.format) && !(dumps.length && entry.format==='lammps-data'));
    if(candidates.length!==1)throw Error('请每次选择一条轨迹（或一个结构），可同时选择它的 LAMMPS data、输入文件和日志；不自动拼接多条轨迹。');
    const primary=candidates[0],file=primary.file;
    const selectedFormat=files.length===1 && format!=='auto'?format:primary.format;
    const other=entries.filter(item=>item!==primary).map(item=>item.file);
    if (cells.length) {
      if (cells.length!==1 || other.length!==1 || file.openIndexed || !/\.(?:xyz|extxyz)$/i.test(file.name)) {
        throw Error('请选择一条 CP2K XYZ 和一个对应的 CELL 文件。');
      }
      if (cells[0].file.size>32*1024*1024) throw Error('CELL 文件超过 32 MiB。');
      return openCp2k(file,cells[0].file);
    }
    if(other.length && !selectedFormat.startsWith('lammps'))throw Error('联合读取支持 LAMMPS 配套文件和 CP2K XYZ + CELL。');
    const name=localName(file.name,selectedFormat==='local'?'auto':selectedFormat);
    if(name)return {name:file.name,store:await StructureTrajectory.local(file,name),note:''};
    const companions=await companionsText(other);
    if(['gromacs','lammps-dump-text'].includes(selectedFormat)) {
      const metadata=remoteCompanions ? remoteCompanions.then(items=>[...companions,...items]) : companions;
      return {name:file.name,...await StructureMD.open(file,selectedFormat,metadata)};
    }
    if(file.size>32*1024*1024)throw Error('此格式单文件转换上限为 32 MiB。');
    let metadata=null,typeMap='';
    if(selectedFormat==='lammps-data') {
      const all=[...companions,{name:file.name,text:await file.text()}];
      const initial=StructureLammps.resolve(all);
      if(!initial.typeCount || initial.typeCount>1180)throw Error('data 的 atom types 数量无效或过多');
      const types=Array.from({length:initial.typeCount},(_,i)=>String(i+1));
      metadata=StructureLammps.resolve(all,types);
      typeMap=types.map(type=>metadata.mapping[type]).join(' ');
    }
    const atomStyle=['bond','angle'].includes(metadata?.atomStyle)?'molecular':metadata?.atomStyle||'auto';
    const query=new URLSearchParams({filename:file.name,format:selectedFormat,type_map:typeMap,units:metadata?.units||'metal',atom_style:atomStyle});
    const response=await fetch(`/structures/convert?${query}`,{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:file instanceof Blob ? file : await file.slice(0,file.size).blob()});
    if(!response.ok) {
      const result=await response.json().catch(()=>({}));
      throw Error(response.status===401?'登录已过期，请返回 Chem Code 重新登录。':result.error?.message||`读取失败（HTTP ${response.status}）`);
    }
    let blob=await response.blob();
    let note='';
    if(metadata) {
      if(metadata.pbc)blob=new Blob([(await blob.text()).replace(/pbc="[TF ]+"/g,`pbc="${metadata.pbc.map(value=>value?'T':'F').join(' ')}"`)]);
      note+=`自动识别 ${typeMap} · ${metadata.pbc?'使用配套文件的边界条件':'data 未包含边界条件，按三维周期显示'}`;
      if(metadata.warnings.length)note+=' · '+metadata.warnings.join('；');
    }
    return {name:file.name,store:await StructureTrajectory.local(blob,'converted.extxyz'),note};
  }
  const api={localName,open,classify,openSequence};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;else root.StructureImport=api;
})(typeof window!=='undefined'?window:globalThis);
