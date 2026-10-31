/* Browser file routing, separate from rendering and from the original MCP transport. */
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
  async function open(file, {format = 'auto', typeMap = '', units = 'metal', atomStyle = 'auto'} = {}) {
    const name = localName(file.name, format);
    if (name) return {store: await StructureTrajectory.local(file, name), note: '本地浏览器读取 · 坐标单位 Å · 未上传文件'};
    if (file.size > 32 * 1024 * 1024) throw Error('转换上传上限为 32 MiB，请先拆分文件。XYZ / VASP / ASE .traj 可直接本地读取。');
    const query = new URLSearchParams({filename:file.name, format, type_map:typeMap, units, atom_style:atomStyle});
    const response = await fetch(`/structures/convert?${query}`, {
      method:'POST', headers:{'Content-Type':'application/octet-stream'}, body:file,
    });
    if (!response.ok) {
      const result = await response.json().catch(() => ({}));
      throw Error(response.status === 401 ? '登录已过期，请返回化学助手重新登录。' : result.error?.message || `读取失败（HTTP ${response.status}）`);
    }
    const blob = await response.blob();
    const selected = response.headers.get('X-Structure-Format');
    let note = '当前服务转换 · 坐标单位 Å · 未发送给模型';
    if (selected === 'gromacs') note += ' · GRO 元素由原子名推断，请核对；nm 已转为 Å';
    if (selected === 'lammps-data') note += ' · data 不包含边界条件，按三维周期显示';
    if (selected?.startsWith('lammps')) note += ' · 晶胞原点平移至零，原文件未修改';
    return {store: await StructureTrajectory.local(blob, 'converted.extxyz'), note};
  }
  const api = {localName, open};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.StructureImport = api;
})(typeof window !== 'undefined' ? window : globalThis);
