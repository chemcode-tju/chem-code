/* Bounded reads of modern ASE ULM files selected in the local file picker. */
const ASETrajectory = (() => {
  const elements='X H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr Rf Db Sg Bh Hs Mt Ds Rg Cn Nh Fl Mc Lv Ts Og'.split(' ');
  async function open(blob) {
    async function read(offset,size) {
      if(!Number.isSafeInteger(offset)||offset<0||!Number.isSafeInteger(size)||size<0||size>8388608||offset+size>blob.size)throw Error('ASE 轨迹数据块无效或不完整');
      return new DataView(await blob.slice(offset,offset+size).arrayBuffer());
    }
    const text=v=>new TextDecoder().decode(new Uint8Array(v.buffer,v.byteOffset,v.byteLength)), integer=(v,i)=>Number(v.getBigInt64(i,true));
    const h=await read(0,48);
    if(!['- of Ulm','AFFormat'].includes(text(new DataView(h.buffer,0,8))) || new TextDecoder().decode(new Uint8Array(h.buffer,8,16)).trim()!=='ASE-Trajectory')throw Error('需要现代 ASE .traj 文件，不支持 pickle 轨迹');
    const version=integer(h,24),count=integer(h,32),table=integer(h,40);
    if(![1,2,3].includes(version)||!Number.isSafeInteger(count)||count<1)throw Error('无效 ASE 轨迹版本或帧数');
    async function meta(index){const offset=integer(await read(table+8*index,8),0),size=integer(await read(offset,8),0);return JSON.parse(text(await read(offset+8,size)));}
    async function array(data,key){
      const [shape,dtype,offset]=data[key+'.'].ndarray,formats={int64:[8,'getBigInt64'],int32:[4,'getInt32'],float64:[8,'getFloat64'],float32:[4,'getFloat32']};
      if(!formats[dtype]||!shape.length||shape.some(n=>!Number.isInteger(n)||n<0))throw Error('不支持的 ASE 数组');
      const [size,method]=formats[dtype],n=shape.reduce((a,b)=>a*b,1),buffer=await read(offset,n*size);
      const values=Array.from({length:n},(_,i)=>Number(buffer[method](i*size,data._little_endian!==false)));
      if(shape.length===1)return values;
      if(shape.length!==2)throw Error('无效 ASE 数组维数');
      return Array.from({length:shape[0]},(_,i)=>values.slice(i*shape[1],(i+1)*shape[1]));
    }
    const first=await meta(0);
    return {count, async frameText(index){
      const data=await meta(index),header=data['numbers.']?data:first;
      const numbers=await array(header,'numbers'),positions=await array(data,'positions');
      if(numbers.length!==positions.length||!numbers.length||numbers.length>100000)throw Error('ASE 原子数无效');
      const masses=header['masses.']?await array(header,'masses'):null;
      if(masses&&(masses.length!==numbers.length||masses.some(m=>!Number.isFinite(m)||m<=0)))throw Error('ASE 原子质量无效');
      const cell=data.cell||[[0,0,0],[0,0,0],[0,0,0]],pbc=header.pbc||[false,false,false];
      const rows=[numbers.length,`Lattice="${cell.flat().join(' ')}" Properties=species:S:1:pos:R:3${masses?':mass:R:1':''} pbc="${pbc.map(v=>v?'T':'F').join(' ')}"`];
      numbers.forEach((z,i)=>{if(!Number.isInteger(z)||z<1||!elements[z]||positions[i].length!==3||positions[i].some(v=>!Number.isFinite(v)))throw Error('ASE 元素或坐标无效');rows.push(`${elements[z]} ${positions[i].join(' ')}${masses?' '+masses[i]:''}`);});
      return rows.join('\n')+'\n';
    }};
  }
  return {open};
})();
if(typeof module!=='undefined'&&module.exports)module.exports=ASETrajectory;
