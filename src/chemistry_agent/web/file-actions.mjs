export const fileIcon = name => `<svg class="icon" aria-hidden="true"><use href="/assets/ui-icons.svg#i-${name}"/></svg>`;

export function updateStructureCount() {
  const library=document.getElementById('structure-library');
  if(!library)return;
  const count=library.querySelectorAll('.workspace-file,.structure-item').length;
  document.getElementById('structure-file-count').textContent=String(count);
  library.hidden=count===0 && !library.querySelector('.model-record');
}
