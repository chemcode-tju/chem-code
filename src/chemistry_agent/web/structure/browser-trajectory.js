/* Read only the opening snapshot, with on-demand frame ranges and bounded caching. */
const StructureBrowserTrajectory = (() => {
  const MiB = 1024 * 1024;
  const blockSize = 2 * MiB;
  const maxBlocks = 128;
  const playbackCache = {maxFrames:160, maxBytes:256 * MiB, readAhead:72};
  function open({name, url}, signal) {
    const endpoint = new URL(url, location.origin);
    if (endpoint.origin !== location.origin || endpoint.pathname !== '/api/file-browser/trajectory') {
      throw Error('轨迹读取地址无效');
    }
    if (typeof name !== 'string' || !name) throw Error('轨迹文件名无效');
    let snapshot;
    async function request(offset, length, {priority = 'auto', ...query} = {}) {
      const target = new URL(endpoint);
      for (const [key, value] of Object.entries({offset, length, ...query})) {
        target.searchParams.set(key, value);
      }
      if (snapshot) target.searchParams.set('snapshot', snapshot);
      const response = await fetch(target, {signal, priority, credentials:'same-origin', redirect:'error'});
      if (!response.ok) {
        const detail = await response.json().catch(() => null);
        throw Error(detail?.error?.message || '轨迹读取失败。');
      }
      return response;
    }
    if (/\.(?:xyz|extxyz)$/i.test(name)) {
      const spans = [];
      let offset = 0;
      const source = {
        get count() {return spans.length;},
        complete:false,
        scanDelay:0,
        async advance(first = false) {
          // Make the first playable batch available quickly, then switch to
          // the larger server-side scan batches for the rest of the timeline.
          const length = first ? 256 * 1024 : spans.length === 1 ? 4 * MiB : 8 * MiB;
          const response = await request(offset, length, {index:true, first, priority:first?'high':'low'});
          const batch = await response.json();
          snapshot = batch.snapshot;
          offset = batch.nextOffset;
          spans.push(...batch.spans);
          source.complete = offset === batch.size;
          return batch.text;
        },
        async read(index) {
          const [start, end] = spans[index];
          return (await request(start, end - start, {priority:'high'})).text();
        },
        async readMany(indices, onFrame) {
          const batches=[];
          let batch;
          for(const index of indices) {
            const [start,end]=spans[index];
            // Keep each read close to the requested frames, including at a large stride.
            if(!batch || end - batch.start > 4 * MiB || start - batch.end > 512 * 1024) {
              batch={start,end,indices:[]};
              batches.push(batch);
            }
            batch.end=end;
            batch.indices.push(index);
          }
          async function readBatch({start,end,indices:frames}) {
            const blob=await (await request(start,end-start)).blob();
            // A ready batch is playable immediately, even while its neighbor is loading.
            await Promise.all(frames.map(async index=>{
              const [begin,stop]=spans[index];
              onFrame(index,await blob.slice(begin-start,stop-start).text());
            }));
          }
          for(let cursor=0;cursor<batches.length;cursor+=2) {
            const results=await Promise.allSettled(batches.slice(cursor,cursor+2).map(readBatch));
            if(results.some(result=>result.status==='rejected'))break;
          }
        },
      };
      return {name, async openIndexed() {
        const text = await source.advance(true);
        const store = new StructureTrajectory.FrameStore(name, source, playbackCache);
        store.put(0, text);
        return store;
      }};
    }

    // The indexer and the player share aligned byte blocks and in-flight reads.
    // A complete scan of a 120 MiB XDATCAR then leaves its data in local memory.
    const cached = new Map();
    const pending = new Map();
    let prefix;
    function block(start, priority = 'auto') {
      if (cached.has(start)) {
        const blob = cached.get(start);
        cached.delete(start);
        cached.set(start, blob);
        return Promise.resolve(blob);
      }
      if (!pending.has(start)) {
        const length = Math.min(blockSize, file.size - start);
        const task = request(start, length, {priority}).then(response => response.blob()).then(blob => {
          if (blob.size !== length) throw Error('轨迹数据块不完整。');
          if (signal?.aborted) return blob;
          if (start === 0) prefix = null;
          cached.set(start, blob);
          while (cached.size > maxBlocks) cached.delete(cached.keys().next().value);
          return blob;
        }).finally(() => pending.delete(start));
        pending.set(start, task);
      }
      return pending.get(start);
    }
    const file = {
      name,
      size:0,
      frozen:true,
      playbackCache,
      slice(start, end) {
        if (prefix && end <= prefix.size) return prefix.slice(start, end);
        async function part() {
          const stop = Math.min(end, file.size);
          if (stop <= start) return new Blob();
          const parts = [];
          let offset = Math.floor(start / blockSize) * blockSize;
          while (offset < stop) {
            const blob = await block(offset, 'high');
            parts.push(blob.slice(Math.max(0, start - offset), stop - offset));
            offset += blockSize;
          }
          return parts.length === 1 ? parts[0] : new Blob(parts);
        }
        return {blob:part, text:async () => (await part()).text(), arrayBuffer:async () => (await part()).arrayBuffer()};
      },
      async warm(start, end) {
        let offset = Math.floor(start / blockSize) * blockSize;
        const stop = Math.min(end, file.size);
        while (offset < stop) {
          await Promise.all([offset, offset + blockSize].filter(position=>position < stop).map(position=>block(position)));
          offset += blockSize * 2;
        }
      },
      text() {return file.slice(0, file.size).text();},
    };
    return {name, async initialize() {
      const response = await request(0, 64 * 1024);
      snapshot = response.headers.get('X-Trajectory-Snapshot');
      file.size = Number(response.headers.get('X-Trajectory-Size'));
      // A small bootstrap keeps the first frame responsive. Promote the
      // prefix into its aligned block in the background after initialization.
      prefix = await response.blob();
      if (prefix.size === Math.min(blockSize, file.size)) {
        cached.set(0, prefix);
        prefix = null;
      }
      return file;
    }};
  }
  return {open};
})();
