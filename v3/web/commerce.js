let commerceTemplates=[], commerceSelected=null;
const commerceLabels={permission_denied:"权限拒绝",unknown_reconciliation:"结果未知，待核对",result_stale:"结果待更新",cancelled:"已取消或拒绝提案",execution_failed:"执行失败",business_failed:"业务校验未通过",approval_expired:"审批已过期",needs_input:"待补充资料",in_progress:"执行中",paused:"已暂停",unverified:"尚未完成业务校验",report_ready:"调查通过",awaiting_repair:"可申请修复",draft_pr_delivered:"Draft PR 已交付",publishing:"发布中",awaiting_approval:"等待审阅完整提案",needs_review:"需要复核"};
function commerceText(parent,tag,text){const n=document.createElement(tag);n.textContent=text;parent.append(n);return n;}
function localTime(value){const d=new Date(value);return new Date(d.getTime()-d.getTimezoneOffset()*60000).toISOString().slice(0,16);}
function commerceTemplate(){const t=commerceTemplates.find(t=>t.template_id===$('commerce-template').value);$('commerce-mode').textContent=t ? (t.execution_mode==='live'?'真实模型与固定 GitHub 提交':'合成数据、本地 Git 快照与受控执行') : '请先由服务端注册来源。';if(t){$('commerce-start').value=localTime(t.window.start);$('commerce-end').value=localTime(t.window.end);}}
async function refreshCommerce(){
  const [templates,cases]=await Promise.all([api('/commerce/templates'),api('/commerce/cases')]);
  if(JSON.stringify(templates)!==JSON.stringify(commerceTemplates)){commerceTemplates=templates;$('commerce-template').replaceChildren();for(const t of templates){const o=document.createElement('option');o.value=t.template_id;o.textContent={duplicate_paid:'重复支付事件',status_filter:'支付状态过滤',refunded_status:'退款状态核对'}[t.template_id]||t.template_id;o.disabled=!t.enabled;$('commerce-template').append(o);}commerceTemplate();}
  $('commerce-submit').disabled=!templates.some(t=>t.enabled);
  $('commerce-cases').replaceChildren();for(const c of cases){const v=c.view;const b=button(`${c.case_id.slice(0,8)} · ${commerceLabels[v.rendered.status]} · ${v.phase==='repair'?'修复':'调查'}`,()=>showCommerce(c.case_id));$('commerce-cases').append(b);}
  if(commerceSelected)await showCommerce(commerceSelected);
}
async function showCommerce(id){
  commerceSelected=id;const [data,detail]=await Promise.all([api(`/commerce/cases/${id}/result`),api(`/commerce/cases/${id}`)]);
  const host=$('commerce-detail');host.hidden=false;host.replaceChildren();const v=data.view,r=data.result;
  commerceText(host,'h3',commerceLabels[v.rendered.status]);
  commerceText(host,'p',`执行：${labels[v.execution.status]||v.execution.status} · 业务：${v.business.status} · 审批：${v.approval} · 交付：${v.delivery}`);
  commerceText(host,'p',`工单版本 ${v.case_revision} · ${detail.spec.task_kind} · ${detail.spec.window.start} 至 ${detail.spec.window.end} · ${detail.spec.repository}#${detail.spec.issue_number}`);
  if(r){commerceText(host,'pre',JSON.stringify({判定编号:r.result_id,检查器:r.checker_version,事实:r.facts,检查:r.checks,原因:r.reason_codes,缺失:r.missing_inputs},null,2));
    if(r.artifact_hash)host.append(button(v.phase==='repair'?'读取已验证补丁':'读取调查报告',async()=>commerceText(host,'pre',await api(`/commerce/cases/${id}/artifacts/${r.artifact_hash}`))));
    for(const q of r.quote_refs)host.append(button(`读取${q.unit_kind==='code_lines'?'源码':'知识'}引用 ${q.start_line}–${q.end_line} 行`,async()=>commerceText(host,'pre',await api(`/commerce/cases/${id}/artifacts/${q.content_hash}`))));}
  if(v.rendered.can_request_repair)host.append(button('请求独立修复提案',async()=>{await api(`/commerce/cases/${id}/repair`,{expected_case_revision:v.case_revision,investigation_result_hash:data.result_hash,client_request_id:crypto.randomUUID()});await refreshCommerce();}));
  for(const a of data.actions){commerceText(host,'h4',`Draft PR · ${a.status}`);commerceText(host,'pre',a.payload.diff);commerceText(host,'pre',JSON.stringify({仓库:a.payload.repository,基础提交:a.payload.base_sha,分支:a.payload.branch,公开测试:a.payload.public_receipt,独立测试:a.payload.hidden_receipt,提案哈希:a.payload_hash},null,2));
    if(v.rendered.can_approve&&a.status==='proposed')host.append(button('批准这份完整提案',async()=>{await api(`/runs/${a.run_id}/approvals`,{action_id:a.action_id,payload_hash:a.payload_hash});await refreshCommerce();}));
    if(a.status==='outcome_unknown')host.append(button('只读核对交付结果',async()=>{await api(`/runs/${a.run_id}/actions/${a.action_id}/reconcile`,{});await refreshCommerce();}));
    if(a.external_id)commerceText(host,'p',`交付回执：${a.external_id}`);
  }
  const message=document.createElement('textarea');message.rows=3;message.setAttribute('aria-label','补充工单信息');host.append(message,button('提交补充信息',async()=>{await api(`/commerce/cases/${id}/messages`,{body:message.value,client_request_id:crypto.randomUUID(),expected_case_revision:v.case_revision});await refreshCommerce();}));
  commerceText(host,'p','修复阶段收到新业务信息将使旧提案失效，并开启新版本调查；未知请求须先完成核对。补丁编辑与任务控制可从下方原工单执行记录进入。');
  for(const link of detail.runs)host.append(button(`版本 ${link.revision} · ${link.stage} · 查看执行记录`,async()=>{const run=await api(`/runs/${link.run_id}`);await show(run.ticket_id);}));
}
$('commerce-template').addEventListener('change',commerceTemplate);
$('commerce-form').addEventListener('submit',async event=>{event.preventDefault();try{const t=commerceTemplates.find(t=>t.template_id===$('commerce-template').value);const receipt=await api('/commerce/cases',{template_id:t.template_id,issue_number:Number($('commerce-issue').value),window:{start:new Date($('commerce-start').value).toISOString(),end:new Date($('commerce-end').value).toISOString()},goal:$('commerce-goal').value,execution_mode:t.execution_mode,client_request_id:crypto.randomUUID()});await refreshCommerce();await showCommerce(receipt.case_id);}catch(error){notice(error.message);}});
