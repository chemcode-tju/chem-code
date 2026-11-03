/* Translate only UI presentation, never structure data, paths or input values.
 * Modules keep their original diagnostic strings; one catalogue covers both
 * JavaScript and Python errors at the presentation boundary. */
(function(root) {
  'use strict';
  const catalog = typeof module !== 'undefined' && module.exports
    ? require('./locales.json') : {
  "← 化学助手": "← Chemistry Agent",
  "文件格式与导入选项": "File format and import options",
  "查看选项": "View options",
  "自动识别": "Detect automatically",
  "选择结构文件开始查看；LAMMPS 支持多选或一起拖入配套文件。": "Open a structure file. For LAMMPS, select or drop the trajectory and its companion files together.",
  "LAMMPS：同时选择轨迹及配套 data/拓扑、输入文件或日志，自动识别元素、单位与原子类型。其他格式每次选择一个文件。": "For LAMMPS, select the trajectory together with its data file, topology, input or log to identify elements, units and atom types. For other formats, choose one file at a time.",
  "GRO、LAMMPS dump、XYZ、VASP、ASE .traj 在本地逐帧读取；data/PDB/CIF 由当前服务转换。GRO 元素依据原子名推断；暂不支持 XTC / TRR / TPR。": "GRO, LAMMPS dump, XYZ, VASP and ASE .traj stream locally. The server converts data, PDB and CIF files. GRO elements are inferred from atom names. XTC, TRR and TPR are unsupported.",
  "本地浏览器读取 · 坐标单位 Å · 未上传文件": "Read locally · Coordinates in Å · File stays in this browser",
  "正在读取…": "Reading…",
  "读取失败，请检查文件或导入选项后重新选择。": "Could not read the file. Check the import options and try again.",
  "按路径打开": "Open path",
  "本地文件": "Local file",
  "快照": "Snapshot",
  "导出动画": "Export animation",
  "显示设置": "Display settings",
  "源码": "Source",
  "显示方式": "Representation",
  "原子球": "Spheres",
  "球棍": "Ball and stick",
  "线棒": "Sticks",
  "原子配色": "Atom colors",
  "原子半径比例": "Atom radius scale",
  "晶胞": "Cell",
  "序号": "Atom IDs",
  "氢键": "Hydrogen bonds",
  "清除选择": "Clear selection",
  "当前帧源码": "Current frame source",
  "当前结构源码": "Structure source",
  "帧号": "Frame number",
  "轨迹帧滑块": "Trajectory timeline",
  "第一帧": "First frame",
  "上一帧": "Previous frame",
  "播放轨迹": "Play trajectory",
  "暂停轨迹": "Pause trajectory",
  "播放 / 暂停": "Play / pause",
  "下一帧": "Next frame",
  "最后一帧": "Last frame",
  "播放设置": "Playback settings",
  "视图布局": "Viewport layout",
  "单视图": "Single view",
  "双视图": "Two views",
  "四视图": "Four views",
  "视图操作": "View controls",
  "旋转": "Rotate",
  "平移": "Pan",
  "缩放": "Zoom",
  "点击原子选择 / 测量": "Click atoms to select / measure",
  "选择": "Select",
  "框选原子中心（包含后方原子）；Shift 反选": "Select atom centers in a box (including occluded atoms); Shift toggles selection",
  "框选": "Box select",
  "适应视图": "Fit view",
  "展开当前视图": "Expand active viewport",
  "展开 / 恢复多视图": "Expand / restore viewports",
  "质心漂移校正…": "Center-of-mass drift…",
  "质心漂移校正": "Center-of-mass drift",
  "移除轨迹的质心漂移": "Remove center-of-mass drift",
  "参考帧编号（从 0 开始）": "Reference frame (0-based)",
  "计算范围": "Particle group",
  "全部原子": "All atoms",
  "指定原子": "Specified atoms",
  "原子序号（从 1 开始）": "Atom IDs (1-based)",
  "填入当前选择": "Use current selection",
  "未启用": "Disabled",
  "依据原始坐标校正，再应用 Wrap。点“应用”保存原子组，后续选择不会改变它。原始轨迹应使用连续、未折回的坐标。": "Correct original coordinates, then apply Wrap. Apply saves the atom group independently of subsequent selections. The input trajectory should contain continuous, unwrapped coordinates.",
  "关闭": "Close",
  "应用": "Apply",
  "打开结构文件": "Open structure file",
  "当前主机上的完整路径": "Full path on the current host",
  "/完整路径/POSCAR": "/full/path/POSCAR",
  "打开": "Open",
  "元素显隐": "Element visibility",
  "全部显示": "Show all",
  "选中原子": "Selected atoms",
  "保持原样": "Unchanged",
  "配色": "Colors",
  "自定义": "Custom",
  "选中原子颜色": "Selected atom color",
  "显隐": "Visibility",
  "显示": "Show",
  "隐藏": "Hide",
  "应用到选中原子": "Apply to selected atoms",
  "恢复选中原子默认": "Reset selected atoms",
  "恢复所有显示设置": "Reset all display settings",
  "动画": "Animation",
  "每秒帧数": "Frames per second",
  "视窗内播放": "Playback in interactive viewports",
  "播放速度": "Playback speed",
  "每隔 N 帧播放": "Every Nth frame",
  "循环播放": "Loop playback",
  "取消": "Cancel",
  "确定": "OK",
  "保存快照": "Save snapshot",
  "当前构型快照": "Current structure snapshot",
  "文件名": "File name",
  "格式": "Format",
  "透明背景（PNG）": "Transparent background (PNG)",
  "裁去四周空白": "Crop blank margins",
  "保存到": "Save to",
  "本地 Mac": "Local Mac",
  "服务器": "Server",
  "目录": "Directory",
  "结构所在目录": "Structure directory",
  "保存": "Save",
  "起始帧（从 1 开始）": "First frame (1-based)",
  "结束帧": "Last frame",
  "每隔 N 帧导出": "Export every Nth frame",
  "基础帧率": "Base frame rate",
  "导出并保存": "Export and save",
  "坐标轴": "Coordinate axes",
  "当前主机": "Current host",
  "请求超时：{0}": "Request timed out: {0}",
  "第 {0} 帧 VASP 源码": "VASP source — frame {0}",
  "第 {0} 帧源码": "Source — frame {0}",
  "VASP 文件源码": "VASP file source",
  "已选 {0}": "Selected: {0}",
  "选中原子 · {0}": "Selected atoms · {0}",
  "当前帧的原子无法与参考帧对应，已停用质心校正": "Atoms could not be matched to the reference frame; drift correction has been disabled",
  "正在读取第一帧…": "Reading first frame…",
  "分段读取失败": "Could not read trajectory batch",
  "分段读取返回无效数据": "Invalid trajectory batch response",
  "没有有效结构帧": "No valid structure frames",
  "加载停止：{0}（保留已加载的 {1} 帧）": "Loading stopped: {0} ({1} loaded frames retained)",
  "加载停止：{0}": "Loading stopped: {0}",
  "当前宿主不支持轨迹分段读取，请使用“打开”选择本地 .traj 文件": "This host does not support trajectory batches; use Local file to open a .traj file",
  "结构读取失败": "Could not read structure",
  "Codex 未返回文件内容": "Codex did not return file contents",
  "文件超过宿主读取限制或不是文本文件": "The file exceeds the host read limit or is not a text file",
  "完整读取失败：{0}": "Could not read full file: {0}",
  "宿主请求失败": "Host request failed",
  "{0} 上的完整路径": "Full path on {0}",
  "参考帧尚未加载或超出范围": "Reference frame is not loaded or is out of range",
  "所指定原子无法与参考帧对应": "Specified atoms could not be matched to the reference frame",
  "已应用：{0} 个原子 · 参考帧 {1}": "Applied: {0} atoms · reference frame {1}",
  "已连接，等待文件…": "Connected; waiting for a file…",
  "连接失败：{0}": "Connection failed: {0}",
  "无法读取保存位置，请重新打开查看器": "Could not read save destinations; reopen the viewer",
  "服务器 · {0}": "Server · {0}",
  "保存到服务器": "Save to server",
  "选择本地文件夹并保存…": "Choose local folder and save…",
  "请使用字母、数字、中文、空格、下划线或短横线命名": "Use letters, numbers, spaces, underscores or hyphens in the file name",
  "打开失败": "Could not open file",
  "未返回结构文件": "No structure file returned",
  "导出失败": "Export failed",
  "已取消导出": "Export cancelled",
  "无法读取保存位置": "Could not read save destinations",
  "宿主未确认保存结果": "The host did not confirm the save result",
  "已取消保存": "Save cancelled",
  "已保存：{0}": "Saved: {0}",
  "请在 Mac 系统窗口选择保存位置…": "Choose a save location in the macOS dialog…",
  "无法确认保存状态": "Could not confirm save status",
  "已保存到本地 Mac": "Saved to local Mac",
  "Mac 保存失败或助手已断开，请检查保存助手": "Mac save failed or the helper disconnected; check the save helper",
  "等待 Mac 保存结果超时，请检查系统窗口": "Timed out waiting for Mac save; check the system dialog",
  "正在取消…": "Cancelling…",
  "请填写有效的已加载帧范围、帧率和间隔": "Enter a valid loaded frame range, frame rate and stride",
  "取消导出": "Cancel export",
  "结构文件在导出期间改变，请重新导出": "The structure changed during export; start the export again",
  "第 {0} 帧无法对应质心校正原子": "Cannot match drift-correction atoms in frame {0}",
  "渲染 {0} / {1}": "Rendering {0} / {1}",
  "正在编码动画…": "Encoding animation…",
  "编码失败": "Encoding failed",
  "结构没有晶胞，无法进行周期操作": "Periodic operations require a simulation cell",
  "晶胞体积为零，无法进行周期操作": "Cannot apply periodic operations to a zero-volume cell",
  "晶胞过小或过度倾斜，氢键周期搜索范围过大": "The cell is too small or skewed for the periodic hydrogen-bond search",
  "质心校正至少需要一个原子": "Drift correction requires at least one atom",
  "原子质量必须为正数": "Atom masses must be positive",
  "质心校正的原子无法与参考帧对应": "Drift-correction atoms could not be matched to the reference frame",
  "跟随原子 ID 缺失或重复": "The tracked atom ID is missing or duplicated",
  "跟随原子在当前帧中无法对应；请重新指定原子": "The tracked atom cannot be matched in this frame; select it again",
  "VASP 结构帧不完整": "Incomplete VASP structure frame",
  "VASP 元素计数无效": "Invalid VASP element counts",
  "当前视图没有可导出的原子或晶胞": "No atoms or cell to export in the current view",
  "显卡未能创建 {0}×{1} 的画布（实际 {2}×{3}）": "The GPU could not create a {0}×{1} canvas (actual size: {2}×{3})",
  "距离 1–2：{0} Å": "Distance 1–2: {0} Å",
  "角度未定义（原子重合）": "Undefined angle (coincident atoms)",
  "夹角 1–2–3：{0}°": "Angle 1–2–3: {0}°",
  "二面角未定义（相邻三原子共线）": "Undefined dihedral (three adjacent atoms are collinear)",
  "二面角 1–2–3–4：{0}°": "Dihedral 1–2–3–4: {0}°",
  "ASE 轨迹数据块无效或不完整": "Invalid or incomplete ASE trajectory block",
  "需要现代 ASE .traj 文件，不支持 pickle 轨迹": "A modern ASE .traj file is required; pickle trajectories are unsupported",
  "无效 ASE 轨迹版本或帧数": "Invalid ASE trajectory version or frame count",
  "不支持的 ASE 数组": "Unsupported ASE array",
  "无效 ASE 数组维数": "Invalid ASE array dimensions",
  "ASE 原子数无效": "Invalid ASE atom count",
  "ASE 原子质量无效": "Invalid ASE atom masses",
  "ASE 元素或坐标无效": "Invalid ASE elements or coordinates",
  "原子序号请输入 1, 3, 5-12 这样的格式": "Enter atom IDs in the form 1, 3, 5-12",
  "原子序号必须在 1–{0} 之间": "Atom IDs must be between 1 and {0}",
  "请输入原子序号，或点击“填入当前选择”": "Enter atom IDs or click Use current selection",
  "无效数值：{0}": "Invalid number: {0}",
  "Properties 定义不完整": "Incomplete Properties definition",
  "Properties 列数无效": "Invalid Properties column count",
  "pos 必须有 3 列": "pos must have 3 columns",
  "Properties 缺少 species/Z 或 pos 字段": "Properties must include species/Z and pos fields",
  "第 {0} 行不是原子数": "Line {0} is not an atom count",
  "原子数必须在 1–100000 范围内": "Atom count must be between 1 and 100000",
  "第 {0} 帧不完整": "Incomplete frame {0}",
  "Lattice 必须有 9 个分量": "Lattice must have 9 components",
  "pbc 必须有 1 或 3 个值": "pbc must have 1 or 3 values",
  "原子 {0} 的属性列不完整": "Incomplete property columns for atom {0}",
  "未知元素：{0}": "Unknown element: {0}",
  "VASP 缩放因子必须为 1 或 3 个": "VASP requires 1 or 3 scale factors",
  "晶胞矩阵不完整": "Incomplete cell matrix",
  "晶胞矩阵奇异": "Singular cell matrix",
  "无效缩放因子": "Invalid scale factor",
  "需包含元素名称的 VASP 5/6 格式；不猜测 VASP 4 的元素顺序": "Use VASP 5/6 with element names; VASP 4 element order cannot be inferred",
  "元素计数无效": "Invalid element counts",
  "原子数无效或过大": "Invalid or excessive atom count",
  "无法识别坐标模式：{0}": "Unrecognized coordinate mode: {0}",
  "不支持的文件格式": "Unsupported file format",
  "帧号超出范围": "Frame number is out of range",
  "坐标不完整": "Incomplete coordinates",
  "这里一次只能编辑一帧": "Only one frame can be edited at a time",
  "VASP 请通过源码编辑坐标，保留 Direct/Cartesian 与约束标记": "Edit VASP source coordinates while preserving Direct/Cartesian and constraint flags",
  "无效原子序号或坐标": "Invalid atom ID or coordinates",
  "请输入当前主机上的完整绝对路径。": "Enter a full absolute path on the current host.",
  "请选择 POSCAR、CONTCAR、XDATCAR 或 XYZ/extxyz/traj/VASP 结构文件。": "Choose a POSCAR, CONTCAR, XDATCAR or XYZ/extxyz/traj/VASP structure file.",
  "只能打开普通结构文件。": "Only regular structure files can be opened.",
  "当前主机 {0} 上没有此文件：{1}": "File not found on {0}: {1}",
  "结构文件会话已失效，请重新按路径打开。": "The file session expired; reopen the file by path.",
  "只能读取普通结构文件。": "Only regular structure files can be read.",
  "读取时文件已变化，请等待写入完成后重新打开。": "The file changed while reading; wait for writing to finish and reopen it.",
  "单帧超过 8 MiB 分段预览上限，请使用完整文件模式。": "This frame exceeds the 8 MiB batch preview limit; use full-file mode.",
  "分段读取支持 XYZ/extxyz/ASE traj 和 VASP。": "Batch reading supports XYZ/extxyz/ASE traj and VASP.",
  "无效文件偏移。": "Invalid file offset.",
  "每次读取帧数必须为 1–32。": "Each batch must contain 1–32 frames.",
  "加载期间文件已改变；请重新打开，避免混合不同版本的帧。": "The file changed during loading; reopen it to avoid mixing frame versions.",
  "读取期间文件已改变，请重新打开。": "The file changed while reading; reopen it.",
  "字节 {0} 处不是原子数。": "Expected an atom count at byte {0}.",
  "原子数必须在 1–100000 范围内。": "Atom count must be between 1 and 100000.",
  "轨迹末尾存在不完整帧；请等待写入完成后重新打开。": "The final frame is incomplete; wait for writing to finish and reopen the file.",
  "未知导出操作": "Unknown export action",
  "无效或过大的 ASE 轨迹数据块": "Invalid or excessive ASE trajectory block",
  "ASE 轨迹不完整": "Incomplete ASE trajectory",
  "不支持的 ASE 数组类型：{0} {1}": "Unsupported ASE array type: {0} {1}",
  "ASE 结构数组维度无效": "Invalid ASE structure array dimensions",
  "需要现代 ASE .traj 文件；不读取旧版 pickle 轨迹": "A modern ASE .traj file is required; legacy pickle trajectories are unsupported",
  "ASE 轨迹版本或帧号无效": "Invalid ASE trajectory version or frame number",
  "ASE 原子数与坐标不一致": "ASE atom count does not match coordinates",
  "ASE 晶胞或周期边界无效": "Invalid ASE cell or periodic boundaries",
  "无法打开 Mac 保存窗口：{0}": "Could not open the Mac save dialog: {0}",
  "文件名称只能包含字母、数字、空格、下划线、圆括号和短横线": "File names may contain letters, numbers, spaces, underscores, parentheses and hyphens",
  "文件内容与扩展名不符": "File contents do not match the extension",
  "服务器不能打开 Mac 文件夹窗口，请选择保存到本地 Mac": "Select Local Mac to open the folder chooser on your Mac",
  "请指定保存目录的完整绝对路径": "Specify the full absolute path of the save directory",
  "未知的快照保存位置": "Unknown snapshot destination",
  "保存目录不存在：{0}": "Save directory does not exist: {0}",
  "同名图片过多，请使用其他文件名": "Too many files share this name; use a different name",
  "Mac 保存助手未连接，请在 Mac 打开 Structure Snapshot Saver 后重试": "Mac save helper is disconnected; open Structure Snapshot Saver on your Mac and retry",
  "快照请求不存在或已过期": "The snapshot request is missing or expired",
  "未找到 FFmpeg。请安装视频编码器后重试（Mac: brew install ffmpeg）": "FFmpeg was not found. Install it and retry (Mac: brew install ffmpeg)",
  "导出类型与格式不符": "Export type does not match the format",
  "帧率必须是正数": "Frame rate must be positive",
  "导出任务不存在，请重新导出": "Export job not found; start a new export",
  "导出分块顺序不正确": "Export chunks arrived out of order",
  "动画帧不是有效 PNG": "Animation frame is not a valid PNG",
  "动画各帧尺寸必须一致": "All animation frames must have the same dimensions",
  "动画帧尚未传输完整": "Animation frames have not finished uploading",
  "导出尚未完成": "Export is not complete",
  "VASP 结构帧不完整，请等待文件写入完成后重新打开。": "Incomplete VASP frame; wait for writing to finish and reopen the file.",
  "VASP 元素计数无效；需要含元素名的 VASP 5/6 文件。": "Invalid VASP element counts; use a VASP 5/6 file with element names.",
  "VASP 元素计数无效。": "Invalid VASP element counts.",
  "无法识别 VASP 坐标模式；XDATCAR 当前需要固定晶胞。": "Unrecognized VASP coordinate mode; XDATCAR currently requires a fixed cell.",
  "VASP 结构帧坐标不完整。": "Incomplete VASP frame coordinates.",
  "选择原子": "Select atoms",
  "框选原子": "Box select atoms",
  "点选原子；Shift + 点击可随时选择或取消": "Select atoms; Shift + click toggles selection in any mode",
  "框选原子；Shift + 拖动可随时框选反选": "Box select atoms; Shift + drag toggles atoms in any mode",
  "结构文件已关闭": "The structure file has been closed",
  "帧尚未索引或超出范围": "Frame not yet indexed or out of range",
  "轨迹末尾存在不完整帧": "Incomplete frame at the end of the trajectory",
  "轨迹原子数无效": "Invalid trajectory atom count",
  "无法识别 VASP 坐标模式": "Unrecognized VASP coordinate mode",
  "无效索引请求": "Invalid index request",
  "每次读取帧数必须为 1–{0}。": "Each request must contain 1–{0} frames."
}
;
  const escape = text => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const patterns = Object.entries(catalog).filter(([key])=>/\{\d+\}/.test(key)).map(([key,value])=>{
    const keys=[];
    const parts=key.split(/(\{\d+\})/).map(part=>{
      if(/^\{\d+\}$/.test(part)){keys.push(part);return '([\\s\\S]+?)';}
      return escape(part);
    });
    return {regex:new RegExp('^'+parts.join('')+'$'),keys,value};
  });
  function translate(text, language='en', depth=0) {
    if(language!=='en'||typeof text!=='string'||depth>5||!/[\p{Script=Han}]/u.test(text))return text;
    const trimmed=text.trim();
    let translated=catalog[trimmed];
    if(translated===undefined)for(const {regex,keys,value} of patterns){
      const match=trimmed.match(regex);if(!match)continue;
      translated=value.replace(/\{\d+\}/g,key=>translate(match[keys.indexOf(key)+1],language,depth+1));break;
    }
    return translated===undefined?text:text.slice(0,text.indexOf(trimmed))+translated+text.slice(text.indexOf(trimmed)+trimmed.length);
  }
  const api={translate,catalog,language:'zh-CN'};
  if(typeof module!=='undefined'&&module.exports){module.exports=api;return;}
  root.StructureI18n=api;
  try{api.language=(localStorage.getItem('chemistry.language')||localStorage.getItem('structure-viewer.language'))==='en'?'en':'zh-CN';}catch{}
  const textStates=new WeakMap(),attributeStates=new WeakMap();
  const excluded='script,style,textarea,#filename,#atom-list,[data-no-i18n]';
  function textNode(node){
    if(node.parentElement?.closest(excluded))return;
    const current=node.nodeValue;
    let state=textStates.get(node);
    if(!state||current!==state.last)state={source:current,last:current};
    const value=translate(state.source,api.language);
    state.last=value;textStates.set(node,state);
    if(current!==value)node.nodeValue=value;
  }
  function element(node){
    if(node.matches(excluded))return;
    let states=attributeStates.get(node);if(!states){states={};attributeStates.set(node,states);}
    for(const attr of ['title','aria-label','placeholder']){
      if(!node.hasAttribute(attr))continue;
      const current=node.getAttribute(attr);let state=states[attr];
      if(!state||current!==state.last)state={source:current,last:current};
      const value=translate(state.source,api.language);state.last=value;states[attr]=state;
      if(current!==value)node.setAttribute(attr,value);
    }
  }
  function visit(node){
    if(node.nodeType===3){textNode(node);return;}
    if(node.nodeType!==1||node.matches(excluded))return;
    element(node);for(const child of node.childNodes)visit(child);
  }
  api.setLanguage=language=>{
    api.language=language==='en'?'en':'zh-CN';document.documentElement.lang=api.language;
    try{localStorage.setItem('structure-viewer.language',api.language);localStorage.setItem('chemistry.language',api.language);}catch{}
    visit(document.body);
    const selector=document.getElementById('language');selector.value=api.language;
    selector.options[0].textContent=api.language==='en'?'Chinese (Simplified)':'简体中文';
  };
  document.getElementById('language').onchange=event=>api.setLanguage(event.target.value);
  api.setLanguage(api.language);
  // Observe only text and accessible labels. Rendering canvases and their large
  // WebGL buffers never pass through this observer. Values (including paths)
  // are intentionally excluded. Writes below are idempotent.
  new MutationObserver(records=>{
    for(const record of records){
      if(record.type==='characterData')textNode(record.target);
      else if(record.type==='attributes')element(record.target);
      else for(const node of record.addedNodes)visit(node);
    }
  }).observe(document.body,{subtree:true,childList:true,characterData:true,attributes:true,attributeFilter:['title','aria-label','placeholder']});
})(globalThis);
