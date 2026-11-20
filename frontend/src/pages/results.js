import {plot} from '../components/common/chart.js';
import {feedback, field} from '../components/common/fields.js';
import {empty, metrics, panel, pill} from '../components/common/panels.js';
import {table} from '../components/common/table.js';
import {REVIEW, STATUS, SWAP} from '../constants/enums.js';
import {handler, poll} from '../hooks/action.js';
import {session} from '../stores/session.js';
import {workspace} from '../stores/workspace.js';
import {button, el, h, link, p} from '../utils/dom.js';
import {date, value} from '../utils/format.js';

let openSwap = null;

function reviewForm(item, spec, refresh) {
  const message = feedback();
  const submit = button(spec.action || '提交复核决定',{class:'primary',type:'submit','data-testid':'review-submit'});
  const action = field({key:'decision',label:spec.decision,type:spec.modes?.length ? 'select' : 'string',
    options:spec.modes,required:false,max_length:120});
  return el('form',{class:'fields',onSubmit:handler(submit,message,async event=>{
    const data = new FormData(event.target);
    await workspace.review(item.id,{review:data.get('review'),decision:data.get('decision'),note:data.get('note')});
    await refresh();
  })},el('label',{class:'field',for:'review-state'},el('span',{},'复核结论'),
    el('select',{name:'review',id:'review-state'},el('option',{value:'accepted'},'通过'),el('option',{value:'returned'},'退回'))),
    action,el('label',{class:'field',for:'review-note'},el('span',{},'复核说明'),el('textarea',{id:'review-note',name:'note',required:true,maxlength:500,rows:3})),submit,message);
}

function swapResultView(item, refresh) {
  const result = item.result;
  const message = feedback();
  const redo = session.canWrite && button('按当时数据重跑',{class:'quiet','data-testid':'swap-recompute'});
  if (redo) redo.addEventListener('click',handler(redo,message,async()=>{
    await workspace.recomputeSwap(item.id);
    await refresh();
  }));
  const parts = [
    el('div',{class:'result-head'},p(`复核 #${item.id} · ${date(item.created_at)}`,'eyeline'),h(3,result.headline)),
    el('div',{class:'actions'},pill(SWAP[result.state] || result.state,result.state),redo,message),
    p(result.note,'subtle'),
    metrics([
      {label:'存疑单号',value:result.suspect_count},
      {label:'成熟可互换',value:result.mature_count},
      {label:'最少互换对',value:value(result.min_swaps)},
      {label:'原结论',value:result.original.headline},
    ]),
  ];
  if (result.state === 'flipped') {
    parts.push(p(`翻转后：${result.after.headline}`),
      table([
        {key:'assigned_on',label:'分配日'},
        {key:'cohort',label:'柜点类型'},
        {label:'原 A 单号（改判 B）',render:row=>row.a_id},
        {label:'A 取件',render:row=>row.a_pickup?'是':'—'},
        {label:'A 投诉',render:row=>row.a_complaint?'是':'—'},
        {label:'原 B 单号（改判 A）',render:row=>row.b_id},
        {label:'B 取件',render:row=>row.b_pickup?'是':'—'},
        {label:'B 投诉',render:row=>row.b_complaint?'是':'—'},
      ],result.pairs,{caption:'最少互换方案'}),
      el('details',{},el('summary',{},'翻转后的放量判定'),
        table([
          {key:'gate',label:'检查项'},{key:'rule',label:'放量条件'},
          {key:'observed',label:'观测证据'},{key:'status',label:'状态'},
        ],result.after.gates,{caption:'翻转后的放量判定'})));
  } else {
    parts.push(p(result.reason));
    if (result.reach) parts.push(p(
      `可达范围：A 组窗内取件 ${result.reach.pickup.join('–')} 单、窗内投诉 ${result.reach.complaint.join('–')} 单。`,'subtle'));
    if (result.need) parts.push(p(
      `门槛要求：A 组窗内取件 ${result.need.pickup.join('–')} 单、窗内投诉 ${result.need.complaint.join('–')} 单。`,'subtle'));
  }
  if (result.excluded && result.excluded.length) {
    parts.push(el('details',{},el('summary',{},`不参与互换的存疑单（${result.excluded.length}）`),
      table([{key:'assignment_id',label:'分配单号'},{key:'reason',label:'原因'}],result.excluded,{caption:'不参与互换的存疑单'})));
  }
  if (item.recomputed_at) parts.push(p(`最近重跑：${date(item.recomputed_at)} · 沿用当时的数据批次与圈定范围，结果保持一致。`,'subtle'));
  return el('div',{class:'swap-detail','data-testid':'swap-detail'},parts);
}

function swapForm(job, reviews, refresh) {
  const message = feedback();
  const submit = button('查找最少互换对',{class:'primary',type:'submit','data-testid':'swap-submit'});
  const suspectInput = el('textarea',{id:'swap-suspects',name:'suspects',required:true,rows:5,
    placeholder:'每行一个分配单号，也可用逗号或空格分隔'});
  const datasetSelect = el('select',{id:'swap-dataset',name:'dataset_id'},
    workspace.datasets.map(item=>el('option',{value:item.id,selected:item.id===job.dataset_id},
      `${item.name} · ${item.row_count} 行`)));
  const copySelect = el('select',{id:'swap-copy','aria-label':'选择既有复核'},
    reviews.map(item=>el('option',{value:item.id},`#${item.id} · ${item.suspect_count} 单 · ${date(item.created_at)}`)));
  const copyButton = button('填入既有圈定',{class:'quiet'});
  copyButton.addEventListener('click',handler(copyButton,message,async()=>{
    const detail = await workspace.getSwap(copySelect.value);
    suspectInput.value = detail.suspects.join('\n');
  }));
  return el('form',{class:'fields','data-testid':'swap-form',onSubmit:handler(submit,message,async event=>{
    const data = new FormData(event.target);
    const suspects = String(data.get('suspects') || '').split(/[\s,;、]+/).filter(Boolean);
    if (!suspects.length) throw new Error('请至少圈定一张存疑单');
    const created = await workspace.createSwap(job.id,{suspects,dataset_id:Number(data.get('dataset_id'))});
    openSwap = {run:job.id,id:created.id};
    await refresh();
  })},
    el('label',{class:'field',for:'swap-suspects'},el('span',{},'存疑单号（圈定范围）'),suspectInput,
      el('small',{},'客服标记的可能填反组别的分配单，逐单一行。')),
    el('label',{class:'field',for:'swap-dataset'},el('span',{},'数据批次'),datasetSelect,
      el('small',{},'默认本运行的数据快照；迟到记录补齐并导入新批次后，选择新批次按原圈定范围重算。')),
    reviews.length>0 && el('div',{class:'actions'},copySelect,copyButton),
    el('div',{class:'actions'},submit),message);
}

async function swapPanel(job, refresh) {
  const reviews = await workspace.swaps(job.id);
  const list = table([
    {label:'结论',render:item=>pill(SWAP[item.state] || item.state,item.state)},
    {label:'最少互换对',render:item=>value(item.min_swaps)},
    {key:'suspect_count',label:'存疑单数'},
    {label:'数据批次',render:item=>workspace.datasets.find(entry=>entry.id===item.dataset_id)?.name || `批次 ${item.dataset_id}`},
    {label:'时间',render:item=>date(item.created_at)},
    {label:'操作',render:item=>{
      const view = button('查看',{class:'quiet'});
      view.addEventListener('click',()=>{ openSwap = {run:job.id,id:item.id}; refresh(); });
      return view;
    }},
  ],reviews,{caption:'组别互换复核记录',empty:'还没有互换复核记录。圈定存疑单号后在此查找最少互换对。'});
  let detail = null;
  if (openSwap && openSwap.run === job.id && reviews.some(item=>item.id===openSwap.id)) {
    detail = await workspace.getSwap(openSwap.id);
  }
  return panel('组别互换复核',
    p('客服发现部分分配单的组别可能填反。圈定存疑单号后，在同一分配日、同一柜点类型内寻找能翻转放量结论的最少 A/B 互换对；取件和投诉随单走，每单最多用一次，沿用原观察窗与全部门槛。','subtle'),
    list,
    detail && swapResultView(detail,refresh),
    session.canWrite && swapForm(job,reviews,refresh));
}

export async function results(route, refresh) {
  const {spec,runs,datasets} = workspace;
  const id = route.id || runs[0]?.id;
  const deliveries = spec.scheduled ? await workspace.deliveries() : [];
  const distribution = spec.scheduled && panel('站内收件箱',table([
    {key:'recipient',label:'收件团队'}, {label:'回执',render:item=>pill(item.state==='delivered'?'已投递':'已拦截',item.state)},
    {key:'detail',label:'说明'}, {label:'时间',render:item=>date(item.created_at)},
    {label:'关联结果',render:item=>link(`运行 ${item.run_id}`,`#results/${item.run_id}`)},
  ],deliveries,{caption:'站内收件箱',empty:'计划执行后将在这里显示投递或拦截回执。'}));
  if (!id) return el('div',{},panel('分析结果',empty('还没有可查看的运行结果。','#run','前往运行')),distribution);
  const job = await workspace.getRun(id);
  const source = datasets.find(item => item.id === job.dataset_id);
  const message = feedback();
  const finished = item => item.status === 'complete' || item.status === 'failed';
  if (!finished(job)) {
    const original = location.hash;
    const status = el('p',{role:'status'},'后台正在计算，完成后会自动展示。');
    poll(()=>workspace.getRun(id),finished,()=>location.hash===original)
      .then(result=>{if (result && status.isConnected && location.hash===original) refresh();
        else if(status.isConnected) status.textContent='仍在执行，请点击刷新检查。';})
      .catch(error=>{if(status.isConnected) status.textContent=error.message;});
    return panel(job.name,pill(STATUS[job.status],job.status),status,button('刷新',{class:'quiet',onClick:refresh}));
  }
  const download = button('下载结果 CSV',{class:'quiet','data-testid':'export',onClick:event=>
    handler(download,message,()=>workspace.download(`/runs/${job.id}/export`,`${spec.short_name}-${job.id}.csv`))(event)});
  const history = el('label',{class:'field run-picker',for:'run-picker'},el('span',{},'切换运行记录'),
    el('select',{id:'run-picker',onChange:event=>{location.hash=`#results/${event.target.value}`;}},
      (runs.some(item=>item.id===job.id) ? runs : [job,...runs]).map(item=>el('option',{value:item.id,selected:item.id===job.id},item.name))));
  const result = job.result;
  const analysis = result ? el('section',{class:'analysis','data-testid':'analysis-complete'},
    el('div',{class:'result-head'},p(job.name,'eyeline'),h(2,result.headline),p(result.note,'intro-copy'),
      el('div',{class:'actions'},pill(STATUS[job.status],job.status),download,message)),
    metrics(result.metrics),
    result.charts.map(plot),
    (result.sections || []).map(item=>panel(item.title,p(item.text))),
    result.tables.map(item=>panel(item.title,table(item.columns,item.rows,{caption:item.title}))))
    : panel(job.name,pill(STATUS[job.status],job.status),p(job.error,'feedback'),link('返回调整参数',`#run/${job.dataset_id}`,{class:'quiet'}));
  const inputs = spec.params.map(param=>el('div',{},el('dt',{},param.label),el('dd',{},value(job.params[param.key]))));
  const swapSection = result ? await swapPanel(job,refresh) : null;
  return el('div',{},el('div',{class:'two-col'},el('div',{class:'wide-panel'},analysis,swapSection),
    el('aside',{class:'sidebar-panel'},panel('运行来源',history,
      source && link(source.name,`#inputs/${source.id}`),p(date(job.finished_at),'subtle'),
      el('dl',{class:'dictionary'},inputs)),
    result && panel(spec.decision,pill(REVIEW[job.review],job.review),
      job.review_note && el('div',{class:'decision'},p(job.decision),p(job.review_note)),
      job.review==='pending' && session.canReview && reviewForm(job,spec,refresh)))),distribution);
}
