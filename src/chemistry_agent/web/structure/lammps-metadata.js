/* Read metadata, never execute LAMMPS commands or follow paths from an input file. */
(function(root) {
  'use strict';
  const symbols = new Set(StructureParser.elements.slice(1));
  // Standard atomic weights (ASE); matching is deliberately narrow and reported as inference.
  const weights = {H:1.008,He:4.002602,Li:6.94,Be:9.0121831,B:10.81,C:12.011,N:14.007,O:15.999,F:18.998403163,Ne:20.1797,Na:22.98976928,Mg:24.305,Al:26.9815385,Si:28.085,P:30.973761998,S:32.06,Cl:35.45,Ar:39.948,K:39.0983,Ca:40.078,Sc:44.955908,Ti:47.867,V:50.9415,Cr:51.9961,Mn:54.938044,Fe:55.845,Co:58.933194,Ni:58.6934,Cu:63.546,Zn:65.38,Ga:69.723,Ge:72.63,As:74.921595,Se:78.971,Br:79.904,Kr:83.798,Rb:85.4678,Sr:87.62,Y:88.90584,Zr:91.224,Nb:92.90637,Mo:95.95,Ru:101.07,Rh:102.9055,Pd:106.42,Ag:107.8682,Cd:112.414,In:114.818,Sn:118.71,Sb:121.76,Te:127.6,I:126.90447,Xe:131.293,Cs:132.90545196,Ba:137.327,La:138.90547,Ce:140.116,Pr:140.90766,Nd:144.242,Sm:150.36,Eu:151.964,Gd:157.25,Tb:158.92535,Dy:162.5,Ho:164.93033,Er:167.259,Tm:168.93422,Yb:173.054,Lu:174.9668,Hf:178.49,Ta:180.94788,W:183.84,Re:186.207,Os:190.23,Ir:192.217,Pt:195.084,Au:196.966569,Hg:200.592,Tl:204.38,Pb:207.2,Bi:208.9804,Th:232.0377,Pa:231.03588,U:238.02891};
  const element = value => symbols.has(value) ? value : null;
  function massElement(mass) {
    const matches = Object.entries(weights).filter(([,value]) => Math.abs(value-mass) <= 0.03);
    return matches.length === 1 ? matches[0][0] : null;
  }
  function commentElement(comment) {
    const explicit = comment.match(/\belement\s*[=:]\s*([A-Z][a-z]?)\b/i);
    if (explicit) return element(explicit[1]);
    return element(comment.trim().split(/\s+/)[0]);
  }
  function tokenize(line) {
    return (line.match(/"[^"\n]*"|'[^'\n]*'|[^\s]+/g) || []).map(word => word.replace(/^["']|["']$/g,''));
  }
  const basename = name => name.replaceAll('\\','/').split('/').at(-1);
  const stem = name => basename(name).replace(/^(?:in|input|log)\./i,'').replace(/\.[^.]+$/,'');
  const literal = value => value && !/[$*?\[\]`]/.test(value);
  const commandLines = text => text.replace(/&\s*\r?\n/g,' ').split(/\r?\n/).map(raw=>{
    const [line] = raw.match(/^(?:"[^"]*"|'[^']*'|[^#"'])*/);
    const comment = raw[line.length]==='#' ? raw.slice(line.length+1) : '';
    return {line:line.trim(),comment:comment.trim()};
  });
  const commands = file => commandLines(file.text).filter(row=>row.line).map(row=>tokenize(row.line));

  function selectCompanions(files, name) {
    const data = [], inputs = [];
    for (const file of files) {
      if (/^\s*\d+\s+atom types\s*(?:#.*)?$/m.test(file.text)) data.push(file);
      else {
        const rows = commands(file);
        if (rows.some(row=>/^(?:units|boundary|atom_style|labelmap|mass|pair_coeff|dump|dump_modify|read_data|include)$/.test(row[0]))) {
          inputs.push({file,rows,log:/^log(?:\.|$)|\.log$/i.test(basename(file.name))});
        }
      }
    }
    function referenced(value, candidates, strict = true) {
      if (!literal(value)) return null;
      const path = value.replace(/^\.\//,'');
      const exact = candidates.filter(file=>file.name===path);
      const qualified = path.includes('/') ? candidates.filter(file=>file.name.endsWith('/'+path)) : [];
      const matches = exact.length ? exact : qualified.length ? qualified :
        candidates.filter(file=>basename(file.name)===basename(path));
      if (strict && matches.length>1) throw Error(`配套文件名 ${value} 重复，请只保留本次计算的文件`);
      return matches.length===1 ? matches[0] : null;
    }
    const isTarget = row => row[0]==='dump' && literal(row[5]) && basename(row[5])===basename(name);
    const matched = inputs.filter(input=>input.rows.some(isTarget));
    const inputFiles = inputs.map(item=>item.file);
    const included = new Set(inputs.flatMap(input=>input.rows.filter(row=>row[0]==='include')
      .map(row=>referenced(row[1],inputFiles,false)).filter(Boolean)));
    const roots = inputs.filter(input=>!included.has(input.file) && !input.log);
    const logs = inputs.filter(input=>input.log);
    const named = (roots.length ? roots : logs).filter(input=>stem(input.file.name)===stem(name));
    const targets = matched.length ? matched : named.length ? named : roots.length ? roots : logs;
    if (!matched.length && targets.length>1) {
      throw Error(`无法确定 ${name} 对应的输入或日志：${targets.map(input=>input.file.name).join('、')}`);
    }
    const chosen = new Set(), referredData = new Set();
    function visit(input) {
      if (chosen.has(input)) return;
      chosen.add(input);
      for (const row of input.rows) {
        if (row[0]==='include') {
          const file = referenced(row[1],inputFiles);
          const include = inputs.find(item=>item.file===file);
          if (include) visit(include);
        } else if (row[0]==='read_data') {
          const file = referenced(row[1],data);
          if (file) referredData.add(file);
        }
      }
    }
    targets.forEach(visit);
    const namedData = data.filter(file=>stem(file.name)===stem(name));
    const matchingData = referredData.size ? [...referredData] : namedData.length ? namedData : data;
    if (matchingData.length>1) {
      throw Error(`无法确定 ${name} 对应的 data 文件：${matchingData.map(file=>file.name).join('、')}`);
    }
    return [...matchingData,...[...chosen].map(input=>input.file)];
  }

  function resolve(files, requiredTypes = [], primary = '') {
    const mapping = {}, masses = {}, labels = {}, evidence = {}, warnings = new Set();
    const settings = {}, sources = {};
    function setting(key,value,source) {
      if (settings[key] !== undefined && JSON.stringify(settings[key]) !== JSON.stringify(value)) throw Error(`${key} 信息冲突：${sources[key]} 与 ${source}，请只选择同一次计算的配套文件`);
      settings[key]=value;sources[key]=source;
    }
    function assign(type,symbol,source) {
      if (!element(symbol)) return;
      if (mapping[type] && mapping[type] !== symbol) throw Error(`原子类型 ${type} 的元素冲突：${mapping[type]} / ${symbol}（${source}）`);
      mapping[type]=symbol; evidence[type]=source;
    }
    function label(type,value,source) {
      if (labels[value] && labels[value] !== type) throw Error(`类型标签 ${value} 的编号冲突`);
      labels[value]=type;assign(type,value,source);
    }
    const massRows = [];
    const dumps = primary ? files.flatMap(commands).filter(row=>row[0]==='dump') : [];
    const dumpIds = new Set(dumps.filter(row=>literal(row[5]) && basename(row[5])===basename(primary)).map(row=>row[1]));
    for (const file of files) {
      for(const [type,symbol] of Object.entries(file.elementMap||{}))assign(type,symbol,`${file.name}: element`);
      let section = '';
      const dataUnits = file.text.split(/\r?\n/)[0].match(/LAMMPS data file.*\bunits\s*=\s*(\w+)/);
      if(dataUnits)setting('units',dataUnits[1],file.name);
      for (const {line,comment} of commandLines(file.text)) {
        if (!line) continue;
        if (/^\d+\s+atom types$/.test(line)) setting('typeCount',Number(line.split(/\s+/)[0]),file.name);
        if (/^(Atom Type Labels|Masses|Atoms|Bonds|Angles|Dihedrals|Impropers|Velocities|[A-Za-z ]+ Coeffs)$/.test(line)) {
          section=line;
          if (line==='Atoms' && /^(atomic|full|charge|molecular|bond|angle)$/.test(comment)) setting('atomStyle',comment,file.name);
          continue;
        }
        const words=tokenize(line);
        if (section==='Atom Type Labels' && /^\d+$/.test(words[0]) && words.length===2) label(words[0],words[1],`${file.name}: Atom Type Labels`);
        if (section==='Masses' && words.length===2 && Number.isFinite(Number(words[1]))) massRows.push({type:words[0],mass:Number(words[1]),symbol:commentElement(comment),source:`${file.name}: Masses`});
        if (words[0]==='units' && words.length===2 && !words[1].includes('$')) setting('units',words[1],file.name);
        if (words[0]==='atom_style' && words.length===2 && !words[1].includes('$')) setting('atomStyle',words[1],file.name);
        if (words[0]==='boundary' && words.length===4 && words.slice(1).every(value=>/^[pfsm]{1,2}$/.test(value))) setting('pbc',words.slice(1).map(value => value==='p'||value==='pp'),file.name);
        if (words[0]==='labelmap' && words[1]==='atom') for(let i=2;i+1<words.length;i+=2) if(/^\d+$/.test(words[i]))label(words[i],words[i+1],`${file.name}: labelmap`);
        if (words[0]==='dump_modify' && words.includes('element') && (!dumps.length || dumpIds.has(words[1]))) {
          const values=[];for(let i=words.indexOf('element')+1;i<words.length&&element(words[i]);i++)values.push(words[i]);
          if (values.length && values.every(element)) values.forEach((symbol,i)=>assign(String(i+1),symbol,`${file.name}: dump_modify element`));
        }
        // Only a complete trailing chemical-symbol list is accepted; arbitrary force-field labels are not elements.
        if (words[0]==='pair_coeff' && words[1]==='*' && words[2]==='*') {
          const values=[];for(let i=words.length-1;i>=3&&element(words[i]);i--)values.unshift(words[i]);
          if (values.length && words.length-values.length>3) {
            // Check the complete list against the known type count below, independent of file order.
            massRows.push({pairSymbols:values,source:`${file.name}: pair_coeff`});
          }
        }
        if (words[0]==='mass' && words.length===3 && Number.isFinite(Number(words[2]))) massRows.push({type:words[1],mass:Number(words[2]),symbol:commentElement(comment),source:`${file.name}: mass`});
      }
    }
    const required=[...new Set(requiredTypes.map(String))];
    const numericRequired=required.map(value=>/^\d+$/.test(value)?Number(value):Number(labels[value])).filter(Number.isFinite);
    const typeCount=settings.typeCount || Math.max(0,...numericRequired,...Object.values(labels).map(Number));
    for (const row of massRows) {
      if (row.pairSymbols) {
        if(typeCount && row.pairSymbols.length===typeCount) row.pairSymbols.forEach((symbol,i)=>assign(String(i+1),symbol,row.source));
        continue;
      }
      const type=labels[row.type]||row.type;
      if(!/^\d+$/.test(type))continue;
      if (!(row.mass>0)) throw Error(`类型 ${type} 的质量无效`);
      if (masses[type] && Math.abs(masses[type]-row.mass)>0.03) throw Error(`类型 ${type} 的质量冲突，请核对配套文件`);
      masses[type]=row.mass;
      if(row.symbol)assign(type,row.symbol,row.source+' 注释');
    }
    for(const [type,mass] of Object.entries(masses)) if(!mapping[type]) {
      const symbol=massElement(mass);
      if(symbol){assign(type,symbol,'标准原子量匹配');warnings.add('部分元素由质量推断，请核对；同位素或粗粒化类型需要明确的元素标签');}
    }
    for(const [name,type] of Object.entries(labels)) if(mapping[type]){mapping[name]=mapping[type];if(masses[type])masses[name]=masses[type];}
    const missing=required.filter(type=>!mapping[type]&&!element(type));
    if(missing.length)throw Error(`无法识别原子类型 ${missing.join(', ')}。请同时选择对应的 data/拓扑文件和输入文件或日志；文件须包含元素标签、元素注释或可识别质量。`);
    const units=settings.units || 'metal';
    if(!['metal','real'].includes(units))throw Error(`暂不支持 LAMMPS units ${units}；目前支持 metal / real，lj 无法自动确定真实长度和元素`);
    if(!settings.units)warnings.add('未提供 units，长度按 Å 显示（metal/real）；可同时选择输入文件或日志');
    return {mapping,masses,labels,evidence,units,lengthScale:1,atomStyle:settings.atomStyle||'auto',pbc:settings.pbc,typeCount,warnings:[...warnings]};
  }
  const api={resolve,selectCompanions,element,massElement,weights};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;else root.StructureLammps=api;
})(typeof window!=='undefined'?window:globalThis);
