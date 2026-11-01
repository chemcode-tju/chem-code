import {escapeHtml as esc} from './core.mjs';

export function resultFigures(base, files, title) {
  return (files || []).filter(name => /\.(png|svg|jpe?g|webp)$/i.test(name))
    .map(name => {
      const url = esc(base + '/' + name.split('/').map(encodeURIComponent).join('/'));
      return '<figure class="result-figure"><img loading="lazy" src="' + url
        + '?preview=true" alt="' + esc(title + ' · ' + name)
        + '"><figcaption><span>' + esc(name) + '</span><a download href="'
        + url + '">下载原图</a></figcaption></figure>';
    }).join('');
}

export function planFigures(base, plan) {
  const upstream = new Set(plan.nodes.flatMap(node => node.depends_on));
  return plan.nodes.filter(node => node.kind === 'analysis' && !upstream.has(node.id))
    .map(node => resultFigures(base + '/workflows/' + plan.id + '/artifacts/' + node.id,
      plan.runtime[node.id]?.outputs?.files, node.title)).join('');
}
