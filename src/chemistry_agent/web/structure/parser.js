/* Lossless frame indexing; parse coordinates only for the requested frame. */
(function (root) {
  "use strict";
  const elements = ("X H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr Rf Db Sg Bh Hs Mt Ds Rg Cn Nh Fl Mc Lv Ts Og").split(" ");
  const tokens = line => line.match(/"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|\S+/g) || [];
  const number = value => {
    const result = Number(String(value).replace(/[dD]/g, "e"));
    if (value === undefined || String(value).trim() === "" || !Number.isFinite(result)) throw Error(`无效数值：${value}`);
    return result;
  };
  const vector = line => tokens(line).slice(0, 3).map(number);
  const cartesian = (position, cell) => [0, 1, 2].map(j => position.reduce((sum, value, i) => sum + value * cell[i][j], 0));
  function attributes(header) {
    const result = {};
    const pattern = /([A-Za-z_][\w.-]*)=("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|\{[^}]*\}|\[[^\]]*\]|[^\s]+)/g;
    for (const match of header.matchAll(pattern)) {
      const value = match[2];
      result[match[1]] = /^["'\[{]/.test(value) ? value.slice(1, -1) : value;
    }
    return result;
  }
  function columns(header) {
    const props = attributes(header).Properties;
    if (!props) return { species: 0, position: 1, width: 4 };
    const fields = props.split(":"), result = { species: null, position: null, width: 0 };
    if (fields.length % 3) throw Error("Properties 定义不完整");
    for (let i = 0; i < fields.length; i += 3) {
      const width = number(fields[i + 2]);
      if (!Number.isInteger(width) || width < 1) throw Error("Properties 列数无效");
      if (fields[i] === "species" || fields[i] === "Z") { result.species = result.width; result.atomicNumber = fields[i] === "Z"; }
      if (["id", "atom_id", "identifier"].includes(fields[i]) && width === 1) result.id = result.width;
      if (["mass", "masses", "Mass"].includes(fields[i]) && width === 1) result.mass = result.width;
      const aliases={type:['type','atom_type','atomtype'],typeName:['type_name'],name:['name','atom_name'],residue:['residue','resname'],resid:['resid','residue_id'],sourceId:['source_id'],charge:['charge','q','initial_charges']};
      for(const [key,names] of Object.entries(aliases))if(width===1&&names.includes(fields[i]))result[key]=result.width;
      if (fields[i] === "pos") { if (width !== 3) throw Error("pos 必须有 3 列"); result.position = result.width; }
      result.width += width;
    }
    if (result.species === null || result.position === null) throw Error("Properties 缺少 species/Z 或 pos 字段");
    return result;
  }
  function indexXYZ(lines) {
    const frames = [];
    for (let start = 0; start < lines.length;) {
      if (!lines[start].trim()) { start++; continue; }
      if (!/^\s*\d+\s*$/.test(lines[start])) throw Error(`第 ${start + 1} 行不是原子数`);
      const count = Number(lines[start]);
      if (!count || count > 100000) throw Error("原子数必须在 1–100000 范围内");
      const end = start + count + 2;
      if (end > lines.length || !lines[end - 1].trim()) throw Error(`第 ${frames.length + 1} 帧不完整`);
      frames.push({ start, end, count });
      start = end;
    }
    if (!frames.length) throw Error("没有有效结构帧");
    return frames;
  }
  function parseXYZ(lines, frame) {
    const header = lines[frame.start + 1], info = attributes(header), layout = columns(header);
    const cellValues = info.Lattice ? info.Lattice.trim().split(/\s+/).map(number) : null;
    if (cellValues && cellValues.length !== 9) throw Error("Lattice 必须有 9 个分量");
    const cell = cellValues ? [cellValues.slice(0, 3), cellValues.slice(3, 6), cellValues.slice(6, 9)] : null;
    let pbc = info.pbc ? info.pbc.trim().split(/\s+/).map(v => /^(t|true|1)$/i.test(v)) : [!!cell, !!cell, !!cell];
    if (pbc.length === 1) pbc = [pbc[0], pbc[0], pbc[0]];
    if (pbc.length !== 3) throw Error("pbc 必须有 1 或 3 个值");
    const atoms = [];
    for (let i = 0; i < frame.count; i++) {
      const values = tokens(lines[frame.start + 2 + i]);
      if (values.length < layout.width) throw Error(`原子 ${i + 1} 的属性列不完整`);
      const elem = layout.atomicNumber ? elements[number(values[layout.species])] : values[layout.species].replace(/^['"]|['"]$/g, "");
      if (!elements.includes(elem) || elem === "X") throw Error(`未知元素：${elem}`);
      const [x, y, z] = values.slice(layout.position, layout.position + 3).map(number);
      const atom={ elem, x, y, z, serial: i + 1, ...(layout.mass == null||values[layout.mass]==='""' ? {} : {mass: number(values[layout.mass])}), ...(layout.id == null ? {} : {id: values[layout.id]}) };
      for(const key of ['type','typeName','name','residue','resid','sourceId','charge'])if(layout[key]!=null){
        const value=values[layout[key]];
        if(key==='charge'&&value==='""')continue;
        atom[key]=key==='charge'?number(value):value.startsWith('"')?JSON.parse(value):value.replace(/^'|'$/g,'');
      }
      atoms.push(atom);
    }
    const origin=info.cell_origin?info.cell_origin.split(/\s+/).map(number):[0,0,0];
    if(origin.length!==3)throw Error('Cell origin requires three components');
    return { atoms, cell, pbc, info, header, layout,origin };
  }
  function serializeFrame(atoms,cell,pbc,info={}) {
    // Preserve source identities through the common XYZ transport. Force-field
    // types remain distinct even when several types describe the same element.
    const strings=[['type','atom_type'],['typeName','type_name'],['name','atom_name'],['residue','residue'],['resid','residue_id'],['sourceId','source_id']].filter(([key])=>atoms.some(a=>a[key]!=null));
    const numbers=['mass','charge'].filter(key=>atoms.some(a=>Number.isFinite(a[key])));
    const ids=atoms.every(a=>a.id!=null);
    const properties='species:S:1:pos:R:3'+(ids?':id:I:1':'')+numbers.map(key=>`:${key}:${atoms.every(a=>Number.isFinite(a[key]))?'R':'S'}:1`).join('')+strings.map(([,key])=>`:${key}:S:1`).join('');
    const header=(cell?'Lattice="'+cell.flat().join(' ')+'" ':'')+`Properties=${properties} pbc="${pbc.map(v=>v?'T':'F').join(' ')}"`;
    const attributes=Object.entries(info).map(([key,value])=>`${key}=${JSON.stringify(Array.isArray(value)?value.join(' '):String(value))}`).join(' ');
    const rows=atoms.map(a=>[a.elem,...(a.position||[a.x,a.y,a.z]),...(ids?[a.id]:[]),...numbers.map(key=>Number.isFinite(a[key])?a[key]:'""'),...strings.map(([key])=>JSON.stringify(String(a[key]??'')))].join(' '));
    return `${atoms.length}\n${header} ${attributes}\n${rows.join('\n')}\n`;
  }
  function parseVASP(lines, trajectory) {
    const scales = vector(lines[1]);
    if (![1, 3].includes(scales.length)) throw Error("VASP 缩放因子必须为 1 或 3 个");
    let cell = lines.slice(2, 5).map(vector);
    if (cell.length !== 3 || cell.some(v => v.length !== 3)) throw Error("晶胞矩阵不完整");
    const determinant = cell[0][0] * (cell[1][1] * cell[2][2] - cell[1][2] * cell[2][1]) - cell[0][1] * (cell[1][0] * cell[2][2] - cell[1][2] * cell[2][0]) + cell[0][2] * (cell[1][0] * cell[2][1] - cell[1][1] * cell[2][0]);
    if (Math.abs(determinant) < 1e-12) throw Error("晶胞矩阵奇异");
    if (scales.length === 1 && scales[0] < 0) scales[0] = Math.cbrt(-scales[0] / Math.abs(determinant));
    if (scales.some(v => v <= 0)) throw Error("无效缩放因子");
    const scale = scales.length === 1 ? [scales[0], scales[0], scales[0]] : scales;
    cell = cell.map(v => v.map((x, j) => x * scale[j]));
    const species = tokens(lines[5]);
    if (species.some(s => !elements.includes(s))) throw Error("需包含元素名称的 VASP 5/6 格式；不猜测 VASP 4 的元素顺序");
    const counts = tokens(lines[6]).map(number);
    if (counts.length !== species.length || counts.some(n => !Number.isInteger(n) || n < 0)) throw Error("元素计数无效");
    const symbols = species.flatMap((s, i) => Array(counts[i]).fill(s));
    if (!symbols.length || symbols.length > 100000) throw Error("原子数无效或过大");
    let cursor = 7;
    if (/^s/i.test(lines[cursor].trim())) cursor++;
    const frames = [];
    do {
      while (cursor < lines.length && !lines[cursor].trim()) cursor++;
      if (cursor >= lines.length) break;
      const mode = lines[cursor++].trim();
      if (!/^[dck]/i.test(mode)) throw Error(`无法识别坐标模式：${mode}`);
      const direct = /^d/i.test(mode), start = cursor;
      if (cursor + symbols.length > lines.length) throw Error("VASP 结构帧不完整");
      frames.push({ start, end: start + symbols.length, count: symbols.length, direct });
      cursor += symbols.length;
    } while (trajectory);
    if (!frames.length) throw Error("没有有效结构帧");
    return { frames, cell, symbols, scale };
  }
  class StructureDocument {
    constructor(name, text) {
      this.name = name;
      this.newline = text.includes("\r\n") ? "\r\n" : "\n";
      this.lines = text.split(/\r?\n/);
      this.xyz = /\.(extxyz|xyz)$/i.test(name);
      if (this.xyz) this.frames = indexXYZ(this.lines);
      else if (/^(POSCAR|CONTCAR|XDATCAR)(\.|$)/i.test(name) || /\.(vasp|poscar|contcar|xdatcar)$/i.test(name)) {
        this.vasp = parseVASP(this.lines, /xdatcar/i.test(name)); this.frames = this.vasp.frames;
      } else throw Error("不支持的文件格式");
    }
    get count() { return this.frames.length; }
    frame(index) {
      const frame = this.frames[index];
      if (!frame) throw Error("帧号超出范围");
      if (this.xyz) return parseXYZ(this.lines, frame);
      const { cell, symbols, scale } = this.vasp;
      const atoms = symbols.map((elem, i) => {
        let coords = vector(this.lines[frame.start + i]);
        if (coords.length !== 3) throw Error("坐标不完整");
        coords = frame.direct ? cartesian(coords, cell) : coords.map((x, j) => x * scale[j]);
        const [x, y, z] = coords; return { elem, x, y, z, serial: i + 1 };
      });
      return { atoms, cell, pbc: [true, true, true], info: { comment: this.lines[0] }, header: this.lines[0] };
    }
    frameText(index) {
      if (!this.xyz) return this.serialize();
      const f = this.frames[index]; return this.lines.slice(f.start, f.end).join(this.newline);
    }
    replaceFrame(index, text) {
      if (!this.xyz) return new StructureDocument(this.name, text);
      const replacement = new StructureDocument(this.name, text);
      if (replacement.count !== 1) throw Error("这里一次只能编辑一帧");
      replacement.frame(0);
      const f = this.frames[index], r = replacement.frames[0];
      const lines = [...this.lines.slice(0, f.start), ...replacement.lines.slice(r.start, r.end), ...this.lines.slice(f.end)];
      return new StructureDocument(this.name, lines.join(this.newline));
    }
    updateAtom(frameIndex, atomIndex, coords) {
      if (!this.xyz) throw Error("VASP 请通过源码编辑坐标，保留 Direct/Cartesian 与约束标记");
      const frame = this.frames[frameIndex], parsed = this.frame(frameIndex);
      if (!parsed.atoms[atomIndex] || coords.length !== 3) throw Error("无效原子序号或坐标");
      const values = tokens(this.lines[frame.start + 2 + atomIndex]);
      coords.map(number).forEach((value, j) => { values[parsed.layout.position + j] = value.toFixed(10); });
      this.lines[frame.start + 2 + atomIndex] = values.join(" ");
    }
    serialize() { return this.lines.join(this.newline); }
  }
  class ProgressiveXYZDocument {
    constructor(name) { this.name = name; this.xyz = true; this.frames = []; this.parts = []; }
    get count() { return this.frames.length; }
    append(text) {
      if (!text.trim()) { this.parts.push(text); return; }
      const chunk = new StructureDocument(this.name, text);
      for (let index = 0; index < chunk.count; index++) this.frames.push({ chunk, index });
      this.parts.push(text);
    }
    frame(index) { const ref = this.frames[index]; return ref.chunk.frame(ref.index); }
    frameText(index) { const ref = this.frames[index]; return ref.chunk.frameText(ref.index); }
    serialize() { return this.parts.join(""); }
  }
  class ProgressiveVASPDocument extends ProgressiveXYZDocument {
    constructor(name) { super('XDATCAR'); this.originalName=name; this.xyz=false; this.vaspPreview=true; }
    frameText(index) {
      const {chunk,index:local}=this.frames[index],frame=chunk.frames[local];
      const header=chunk.lines.slice(0,7);
      if(/^s/i.test(chunk.lines[7].trim()))header.push(chunk.lines[7]);
      return [...header,...chunk.lines.slice(frame.start-1,frame.end)].join(chunk.newline)+chunk.newline;
    }
  }
  const api = { StructureDocument, ProgressiveXYZDocument, ProgressiveVASPDocument, attributes, columns, elements, serializeFrame };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.StructureParser = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
