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
    const vasp = /^XDATCAR(?:$|[._-])|\.xdatcar$/i.test(name);
    if (vasp || /\.(?:xyz|extxyz)$/i.test(name)) {
      const spans = [];
      const headers = new Map();
      let offset = 0;
      let headerOffset = 0;
      let rangeSource;
      async function headerFor(index) {
        if (!vasp) return '';
        const [, , start, end] = spans[index];
        if (!headers.has(start)) {
          const pending = request(start,end-start,{priority:'high'}).then(response=>response.text());
          headers.set(start,pending);
          pending.catch(()=>{if(headers.get(start)===pending)headers.delete(start);});
          while (headers.size>64) headers.delete(headers.keys().next().value);
        }
        return headers.get(start);
      }
      function addStep(step) {
        const previous = source.ionicSteps.at(-1);
        if (step !== null && previous != null && step <= previous) {
          source.segmentStart = source.ionicSteps.length;
        }
        source.ionicSteps.push(step);
      }
      async function advanceRanges() {
        const from = rangeSource.count;
        await rangeSource.advance();
        rangeSource.ionicSteps.slice(from).forEach(addStep);
        source.complete = rangeSource.complete;
      }
      async function resumeRanges(size, first) {
        const file = rangedFile(name, size, null, request, signal);
        rangeSource = new StructureTrajectory.LocalSource(file, 'XDATCAR');
        if (!first) {
          rangeSource.resumeVasp(offset, await headerFor(spans.length - 1));
          await advanceRanges();
          return;
        }
        await rangeSource.initialize();
        rangeSource.ionicSteps.forEach(addStep);
        source.complete = rangeSource.complete;
        return rangeSource.read(0);
      }
      const source = {
        get count() {return spans.length + (rangeSource?.count ?? 0);},
        complete:false,
        scanDelay:0,
        ...(vasp ? {ionicSteps:[],segmentStart:0} : {}),
        async advance(first = false) {
          if (rangeSource) return advanceRanges();
          // Make the first playable batch available quickly, then switch to
          // the larger server-side scan batches for the rest of the timeline.
          const length = first ? 256 * 1024 : spans.length === 1 ? 4 * MiB : 8 * MiB;
          const response = await request(offset, length, {
            index:true, first, priority:first?'high':'low', ...(vasp ? {header_offset:headerOffset} : {}),
          });
          const batch = await response.json();
          snapshot = batch.snapshot;
          offset = batch.nextOffset;
          if (vasp) {
            if (batch.rangeFallback === true) return resumeRanges(batch.size, first);
            headerOffset = batch.headerOffset;
            for (const [, , , , step] of batch.spans) addStep(step);
            if (first) {
              const header = batch.text.match(/^(?:[^\n]*\n){7}(?:[ \t]*s[^\n]*\n)?/i)?.[0];
              if (header) headers.set(batch.spans[0][2], header);
            }
          }
          spans.push(...batch.spans);
          source.complete = offset === batch.size;
          return batch.text;
        },
        async read(index) {
          if (index >= spans.length) return rangeSource.read(index - spans.length);
          const [start, end] = spans[index];
          const [header,text] = await Promise.all([
            headerFor(index),
            request(start,end-start,{priority:'high'}).then(response=>response.text()),
          ]);
          return header+text;
        },
        async readMany(indices, onFrame) {
          const batches=[];
          const rangeIndices=[];
          let batch;
          for(const index of indices) {
            if (index >= spans.length) {
              rangeIndices.push(index - spans.length);
              continue;
            }
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
              const [header,text] = await Promise.all([
                headerFor(index),blob.slice(begin-start,stop-start).text(),
              ]);
              onFrame(index,header+text);
            }));
          }
          for(let cursor=0;cursor<batches.length;cursor+=2) {
            const results=await Promise.allSettled(batches.slice(cursor,cursor+2).map(readBatch));
            if(results.some(result=>result.status==='rejected'))break;
          }
          if (rangeIndices.length) {
            await rangeSource.readMany(rangeIndices, (index, text) => onFrame(spans.length + index, text));
          }
        },
      };
      return {name, async openIndexed() {
        const text = await source.advance(true);
        const store = new StructureTrajectory.FrameStore(vasp?'XDATCAR':name, source, playbackCache);
        store.put(0, text);
        return store;
      }};
    }

    return {name, async initialize() {
      const bootstrap = /\.(?:lammpstrj|dump)$/i.test(name) ? blockSize : 64 * 1024;
      const response = await request(0, bootstrap, {priority:'high'});
      snapshot = response.headers.get('X-Trajectory-Snapshot');
      const size = Number(response.headers.get('X-Trajectory-Size'));
      return rangedFile(name, size, await response.blob(), request, signal);
    }};
  }

  function rangedFile(name, size, prefix, request, signal) {
    // Formats scanned by the browser share aligned blocks with playback.
    const cached = new Map();
    const pending = new Map();
    if (prefix?.size === Math.min(blockSize, size)) {
      cached.set(0, prefix);
      prefix = null;
    }
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
      size,
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
    return file;
  }
  return {open};
})();
