import {taskLabel} from './plan-layout.mjs';

const schedulerLabel=state=>({running:'运行中',pending:'排队中',queued:'排队中',completed:'调度完成',failed:'失败',cancelled:'已取消',timeout:'超时',out_of_memory:'内存不足',unknown:'待核对'})[String(state).toLowerCase()] || state || '等待状态';

// A task and its adopted/retried jobs share one inspector. Preserve every job,
// including unsuccessful attempts, without a second navigation tree.
export function executionOptions(plan,node,jobs=[]) {
  const runtime=plan?.runtime || {},accepted=new Set(runtime[node?.id]?.accepted_jobs || []);
  return jobs.map(job=>{
    const owner=plan?.nodes.find(item=>runtime[item.id]?.generic_job_id===job.id);
    const title=owner && owner.id!==node?.id?taskLabel(owner):'';
    return {id:job.id,label:['#'+(job.scheduler_job_id || job.id),schedulerLabel(job.state),accepted.has(job.id)?'已采用':'',title].filter(Boolean).join(' · ')};
  });
}
