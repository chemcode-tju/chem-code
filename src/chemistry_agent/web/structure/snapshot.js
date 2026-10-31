/* Export the active WebGL view, without interface overlays. */
const StructureSnapshot = (() => {
  const encoder=new TextEncoder();
  const concat=parts=>{const out=new Uint8Array(parts.reduce((n,p)=>n+p.length,0));let offset=0;for(const p of parts){out.set(p,offset);offset+=p.length;}return out;};
  const bytesFromURL=url=>Uint8Array.from(atob(url.split(',')[1]),c=>c.charCodeAt(0));
  function crc32(bytes){let crc=0xffffffff;for(const b of bytes){crc^=b;for(let i=0;i<8;i++)crc=(crc>>>1)^((crc&1)?0xedb88320:0);}return (crc^0xffffffff)>>>0;}
  function pngDpi(bytes,dpi){
    const chunk=new Uint8Array(21),view=new DataView(chunk.buffer);view.setUint32(0,9);chunk.set(encoder.encode('pHYs'),4);
    view.setUint32(8,Math.round(dpi/0.0254));view.setUint32(12,Math.round(dpi/0.0254));chunk[16]=1;view.setUint32(17,crc32(chunk.slice(4,17)));
    // Canvas encoders may already include a density chunk. Keep only the new one.
    const parts=[bytes.slice(0,33),chunk];let p=33;
    while(p<bytes.length){const n=new DataView(bytes.buffer,bytes.byteOffset+p,4).getUint32(0)+12;const type=new TextDecoder().decode(bytes.slice(p+4,p+8));if(type!=='pHYs')parts.push(bytes.slice(p,p+n));p+=n;}
    return concat(parts);
  }
  function jpegDpi(bytes,dpi){
    const marker=new Uint8Array([255,224,0,16,74,70,73,70,0,1,1,1,dpi>>8,dpi&255,dpi>>8,dpi&255,0,0]);
    // Replace the existing JFIF segment rather than introducing conflicting metadata.
    if(bytes[2]===255&&bytes[3]===224&&new TextDecoder().decode(bytes.slice(6,11))==='JFIF\0')return concat([bytes.slice(0,2),marker,bytes.slice(4+(bytes[4]<<8|bytes[5]))]);
    return concat([bytes.slice(0,2),marker,bytes.slice(2)]);
  }
  function pdf(jpeg,width,height,dpi){
    const w=width*72/dpi,h=height*72/dpi;
    const commands=`q ${w} 0 0 ${h} 0 0 cm /Im0 Do Q\n`;
    const objects=[encoder.encode('<< /Type /Catalog /Pages 2 0 R >>'),encoder.encode('<< /Type /Pages /Kids [3 0 R] /Count 1 >>'),encoder.encode(`<< /Type /Page /Parent 2 0 R /MediaBox [0 0 ${w} ${h}] /Resources << /XObject << /Im0 4 0 R >> >> /Contents 5 0 R >>`),concat([encoder.encode(`<< /Type /XObject /Subtype /Image /Width ${width} /Height ${height} /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length ${jpeg.length} >>\nstream\n`),jpeg,encoder.encode('\nendstream')]),encoder.encode(`<< /Length ${encoder.encode(commands).length} >>\nstream\n${commands}endstream`)];
    const parts=[encoder.encode('%PDF-1.4\n')],offsets=[0];let size=parts[0].length;
    objects.forEach((obj,i)=>{offsets.push(size);const data=concat([encoder.encode(`${i+1} 0 obj\n`),obj,encoder.encode('\nendobj\n')]);parts.push(data);size+=data.length;});
    parts.push(encoder.encode(`xref\n0 6\n0000000000 65535 f \n${offsets.slice(1).map(n=>`${String(n).padStart(10,'0')} 00000 n \n`).join('')}trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n${size}\n%%EOF\n`));return concat(parts);
  }
  function cropBounds(data,width,height){
    let minX=width,minY=height,maxX=-1,maxY=-1;
    for(let y=0;y<height;y++)for(let x=0;x<width;x++)if(data[(y*width+x)*4+3]>2){minX=Math.min(minX,x);minY=Math.min(minY,y);maxX=Math.max(maxX,x);maxY=Math.max(maxY,y);}
    if(maxX<0)throw Error('当前视图没有可导出的原子或晶胞');
    return {x:minX,y:minY,width:maxX-minX+1,height:maxY-minY+1};
  }
  async function capture(views,{dpi,crop,transparent,format},scene=views.captureScene()) {
    const pixelRatio=scene.pixelRatio,scale=dpi/96;
    const width=Math.round(scene.width*scale),height=Math.round(scene.height*scale);
    const exported=views.exportPane(width/pixelRatio,height/pixelRatio,scene);
    const gl=exported.viewer.getRenderer().getContext();
    const w=gl.drawingBufferWidth,h=gl.drawingBufferHeight;
    if(!w||!h||Math.abs(w-width)>Math.ceil(pixelRatio)||Math.abs(h-height)>Math.ceil(pixelRatio))throw Error(`显卡未能创建 ${width}×${height} 的画布（实际 ${w}×${h}）`);
    const pixels=new Uint8Array(w*h*4);gl.readPixels(0,0,w,h,gl.RGBA,gl.UNSIGNED_BYTE,pixels);
    const raw=document.createElement('canvas');raw.width=w;raw.height=h;
    const ctx=raw.getContext('2d',{willReadFrequently:true}),image=ctx.createImageData(w,h);
    // WebGL readPixels is bottom-up; write explicitly top-down without browser canvas readback ambiguity.
    for(let y=0;y<h;y++)image.data.set(pixels.subarray((h-y-1)*w*4,(h-y)*w*4),y*w*4);
    ctx.putImageData(image,0,0);
    const rect=crop ? cropBounds(ctx.getImageData(0,0,raw.width,raw.height).data,raw.width,raw.height) : {x:0,y:0,width:raw.width,height:raw.height};
    const output=document.createElement('canvas');output.width=rect.width;output.height=rect.height;const painter=output.getContext('2d');
    if(!transparent||format!=='png'){painter.fillStyle='#fff';painter.fillRect(0,0,output.width,output.height);}
    painter.drawImage(raw,rect.x,rect.y,rect.width,rect.height,0,0,rect.width,rect.height);
    let bytes,mime;
    if(format==='png'){bytes=pngDpi(bytesFromURL(output.toDataURL('image/png')),dpi);mime='image/png';}
    else {const jpeg=bytesFromURL(output.toDataURL('image/jpeg',0.97));bytes=format==='pdf'?pdf(jpeg,output.width,output.height,dpi):jpegDpi(jpeg,dpi);mime=format==='pdf'?'application/pdf':'image/jpeg';}
    return {bytes,mime,width:output.width,height:output.height,preview:output.toDataURL('image/png')};
  }
  return {capture,cropBounds,pngDpi,jpegDpi,pdf};
})();
if(typeof module!=='undefined'&&module.exports)module.exports=StructureSnapshot;
