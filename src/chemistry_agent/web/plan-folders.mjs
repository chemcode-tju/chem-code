// Folder membership is presentation metadata. IDs in parents/children always refer to real tasks.
const PAGE = 12;

function neutralBatches(vertices, byId) {
  const batches = new Map();
  const roots = vertices.filter(node => node.primary == null);
  function partition(children, anchor) {
    const ids=children.filter(id=>byId.get(id).groupPath == null);
    if (ids.length <= PAGE) return;
    for (let i = 0; i < ids.length; i += PAGE) {
      const path = [`${i + 1}–${Math.min(i + PAGE, ids.length)}`];
      const batch = {anchor,path};
      for (const id of ids.slice(i,i + PAGE)) batches.set(id,batch);
    }
  }
  partition(roots.map(node => node.id), '');
  for (let root of roots) {
    while (root.tree.length === 1) root = byId.get(root.tree[0]);
    partition(root.tree, String(root.id));
  }
  // Multiple earlier forks can precede a much wider parameter sweep.
  for (const node of vertices) {
    if (node.tree.length > 2*PAGE) partition(node.tree, String(node.id));
  }
  for (const node of vertices) {
    if (node.groupPath != null || batches.has(node.id) || !node.parents.length) continue;
    const batch = batches.get(node.parents[0]);
    if (batch && node.parents.every(id => batches.get(id) === batch)) batches.set(node.id,batch);
  }
  return batches;
}

export function indexFolders(vertices, byId, groups) {
  const batches = neutralBatches(vertices,byId);
  const folders = new Map(), forest = new Map();
  for (const node of vertices) {
    const batch = batches.get(node.id);
    const labels = node.groupPath?.length ? node.groupPath : batch?.path || [];
    node.folderIds = [];
    let parent;
    for (let level = 0; level < labels.length; level++) {
      const path = labels.slice(0,level+1);
      const id = '@'+JSON.stringify([batch ? 'batch:'+batch.anchor : 'folder', ...path]);
      let folder = folders.get(id);
      if (!folder) {
        folder = {id,title:labels[level],path,batch:Boolean(batch),origin:byId.get(batch?.anchor)?.title,depth:node.depth,
          members:[],direct:[],children:[],folder:true};
        folders.set(id,folder);
        if (parent) parent.children.push(folder);
      }
      folder.members.push(node.id);
      node.folderIds.push(id);
      parent=folder;
    }
    if (parent) parent.direct.push(node.id);
    node.section = parent?.id ?? node.group;
  }
  for (const group of groups) {
    const plain = {...group,direct:[],children:[]};
    for (const id of group.members) {
      const node = byId.get(id);
      const folder = folders.get(node.folderIds[0]);
      if (folder) forest.set(folder.id,folder);
      else { plain.direct.push(id); forest.set(plain.id,plain); }
    }
    plain.members=plain.direct;
    plain.depth=byId.get(plain.id).depth;
  }
  const sections=[...forest.values()];
  const filters=[];
  function visit(section) {
    filters.push(section);
    section.children.forEach(visit);
  }
  sections.forEach(visit);
  return {folders,sections,filters};
}

export function inSection(node, scope) {
  return scope == null || node.section === scope || node.folderIds.includes(scope);
}

// A dotted link represents at least one actual dependency hidden by a closed folder.
// Each pair is drawn only once, including at cross-folder joins.
export function folderLinks(graph, represented, folded) {
  const links = new Map();
  for (const node of graph.vertices) {
    const target = represented.get(node.id);
    if (target == null) continue;
    for (const id of node.parents) {
      const source = represented.get(id);
      if (source == null || source===target || folded.has(id)) continue;
      if (!graph.folders.has(source) && !graph.folders.has(target)) continue;
      const key=JSON.stringify([source,target]);
      if (!links.has(key)) links.set(key,{source,target,folder:true});
    }
  }
  return [...links.values()];
}
