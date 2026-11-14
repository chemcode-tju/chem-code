// Presentation only: the persisted plan and its execution dependencies stay intact.
export function workflowView(plan) {
  const index = new Map(plan.nodes.map(node => [node.id, node]));
  const owners = new Map();
  for (const node of plan.nodes) {
    let owner = node;
    const seen = new Set([node.id]);
    while (owner.kind === 'job' && index.has(owner.parent_task)) {
      owner = index.get(owner.parent_task);
      if (seen.has(owner.id)) { owner = node; break; }
      seen.add(owner.id);
    }
    owners.set(node.id, owner.id);
  }
  const children = new Map();
  for (const node of plan.nodes) {
    const owner = owners.get(node.id);
    if (owner === node.id) continue;
    if (!children.has(owner)) children.set(owner, []);
    children.get(owner).push(node);
  }
  const dependencies = new Map();
  for (const node of plan.nodes) {
    const owner = owners.get(node.id);
    if (!dependencies.has(owner)) dependencies.set(owner, new Set());
    for (const parent of node.depends_on || []) {
      const source = owners.get(parent) || parent;
      if (source !== owner) dependencies.get(owner).add(source);
    }
  }
  const nodes = plan.nodes.filter(node => owners.get(node.id) === node.id)
    .map(node => ({...node, depends_on: [...dependencies.get(node.id)]}));
  // Interleaved executions can be acyclic individually but cyclic when grouped.
  // In that case keep the original topology, rather than inventing a stage order.
  const pending=new Map(nodes.map(node=>[node.id,new Set(node.depends_on)]));
  const dependants=new Map();
  for(const node of nodes) for(const id of node.depends_on) {
    if(!dependants.has(id))dependants.set(id,[]);
    dependants.get(id).push(node.id);
  }
  const ready=nodes.filter(node=>!node.depends_on.length).map(node=>node.id);
  for(let i=0;i<ready.length;i++) for(const id of dependants.get(ready[i]) || []) {
    pending.get(id).delete(ready[i]);
    if(!pending.get(id).size)ready.push(id);
  }
  if(ready.length!==nodes.length) return {nodes:plan.nodes,children:new Map(),owners:new Map(plan.nodes.map(node=>[node.id,node.id]))};
  return {nodes, children, owners};
}

export function childJobSummary(plan, children = []) {
  const counts = {};
  for (const node of children) {
    const runtime=plan.runtime[node.id] || {}, job=runtime.job || {};
    const scheduler=String(job.scheduler_state || '').toLowerCase();
    const state = runtime.status==='running' && ['queued','pending','waiting'].includes(scheduler)?'submitted':runtime.status || 'pending';
    counts[state] = (counts[state] || 0) + 1;
  }
  const labels = {completed:'完成', running:'运行', submitted:'排队', failed:'失败', blocked:'阻塞', cancelled:'取消', pending:'待执行'};
  return children.length ? `${children.length} 项作业 · ` + Object.entries(counts)
    .map(([state, count]) => `${count} ${labels[state] || state}`).join(' · ') : '';
}
