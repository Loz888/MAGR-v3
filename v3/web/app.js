let token = "", selected = null, timer = null;
const $ = id => document.getElementById(id);
const labels = {queued:"等待执行",running:"调查中",waiting_approval:"等待审批",waiting_input:"等待补充",paused:"已暂停",succeeded:"已完成",failed:"执行失败",cancelled:"已取消"};
async function api(path, body, method) {
  const response = await fetch("/api/v3" + path, {method: method || (body === undefined ? "GET" : "POST"), headers:{"X-MGAR-Token":token,"Content-Type":"application/json"},body: body === undefined ? undefined : JSON.stringify(body)});
  const text = await response.text();
  if (!response.ok) { let message; try { message=JSON.parse(text).message; } catch { message="请求未完成"; } throw new Error(message); }
  return response.headers.get("content-type")?.includes("application/json") ? JSON.parse(text) : text;
}
function notice(text) { $("notice").textContent=text; }
function button(text, fn) { const node=document.createElement("button"); node.textContent=text; node.className="secondary"; node.addEventListener("click",()=>Promise.resolve(fn()).catch(error=>notice(error.message))); return node; }
async function refresh() {
  const [tickets, status]=await Promise.all([api("/tickets"),api("/router/status")]);
  $("router-status").textContent=`待路由 ${status.pending_events} 条 · Router 实际调用 ${status.physical_router_calls} 次 · ${status.mode === "live" ? "真实模型" : status.mode === "disabled" ? "路由已关闭" : "演示模式"}`;
  $("tickets").replaceChildren();
  if (!tickets.length) { const empty=document.createElement("p"); empty.className="muted"; empty.textContent="还没有工单。提交一个调查问题开始。"; $("tickets").append(empty); }
  for (const ticket of tickets) {
    const node=button("",()=>show(ticket.ticket_id)); node.className="ticket";
    const title=document.createElement("strong"); title.textContent=ticket.question;
    const meta=document.createElement("small"); meta.textContent=`${ticket.external_key} · ${labels[ticket.stage] || ticket.stage} · ${ticket.ticket_id.slice(0,8)}`;
    node.append(title,meta); $("tickets").append(node);
  }
}
async function show(id) {
  selected=id; const ticket=await api("/tickets/"+encodeURIComponent(id));
  $("detail").hidden=false; $("detail-title").textContent=ticket.question;
  $("detail-meta").textContent=`${labels[ticket.stage] || ticket.stage} · 来源：${ticket.channels.join(" / ")} · ${ticket.ticket_id}`;
  $("detail-controls").replaceChildren();
  if (!["succeeded","failed","cancelled"].includes(ticket.stage)) {
    const controls=["r2-mailbox-v1","r4-experts-v1","r5-repair-v1"].includes(ticket.graph_version) ? [ticket.stage === "paused" ? ["恢复","resume"] : ["暂停","pause"],["取消","cancel"]] : [["取消","cancel"]];
    for (const [label,action] of controls) $("detail-controls").append(button(label,async()=>{await api(`/tickets/${id}/${action}`,{}); await refresh(); await show(id);}));
  }
  $("related").replaceChildren();
  for (const related of ticket.related_tickets) $("related").append(button(related.question,()=>show(related.ticket_id)));
  if (!ticket.related_tickets.length) $("related").textContent="暂无关联工单";
  $("events").textContent=await api(`/runs/${ticket.run_id}/events`);
  $("resolution").replaceChildren(); $("memory-controls").replaceChildren();
  const resolution=await api(`/runs/${ticket.run_id}/resolution`);
  if (resolution) {
    for (const [title,items] of [["已核验的观察",resolution.payload.verified_facts.map(f=>f.text)], ["假设",resolution.payload.hypotheses],["冲突",resolution.payload.contradictions],["缺失证据",resolution.payload.missing_evidence],["下一步",resolution.payload.next_steps]]) {
      const heading=document.createElement("h4"); heading.textContent=title; const text=document.createElement("pre"); text.textContent=items.join("\n") || "暂无"; $("resolution").append(heading,text);
    }
    if (["succeeded","failed","cancelled"].includes(ticket.stage) && resolution.verdict.passed) {
      $("memory-controls").append(button("预览可保留的摘要",async()=>{
        const preview=await api(`/tickets/${id}/memory-preview`); const text=document.createElement("pre"); text.textContent=preview.summary; $("memory-controls").replaceChildren(text);
        $("memory-controls").append(button("确认保留这段摘要",async()=>{await api(`/tickets/${id}/memory`,{summary_hash:preview.summary_hash,report_hash:preview.report_hash}); await refreshKnowledge(); notice("摘要已确认保存。");}));
      }));
    }
  }
  const actions=await api(`/runs/${ticket.run_id}/actions`); $("actions").replaceChildren();
  for (const action of actions) {
    const wrap=document.createElement("div"); wrap.className="action"; let edited=null;
    const state=document.createElement("p"); state.textContent=`${action.reply_kind === "system" ? "系统回复" : "调查结果"} · ${action.status}`;
    const preview=document.createElement("pre"); preview.textContent=action.payload.body; wrap.append(state,preview);
    if (action.action_type === "draft_pr") {
      state.textContent=`Draft PR · ${action.status} · ${action.payload.source_origin === "fixture_git" ? "本地协议演示" : "GitHub"}`;
      const proof=document.createElement("pre"); proof.textContent=JSON.stringify({repository:action.payload.repository,base:action.payload.base_sha,branch:action.payload.branch,expires_at:action.expires_at,public_tests:action.payload.public_receipt,independent_tests:action.payload.hidden_receipt},null,2); wrap.append(proof);
      for (const receipt of [action.payload.public_receipt,action.payload.hidden_receipt]) wrap.append(button("读取测试记录",async()=>{const text=document.createElement("pre");text.textContent=await api(`/runs/${ticket.run_id}/repair/artifacts/${receipt.log_hash}`);wrap.append(text);}));
      if (action.status === "proposed") {
        const edit=document.createElement("textarea");edit.value=action.payload.diff;edit.rows=12;edit.setAttribute("aria-label","编辑完整补丁，提交后重新测试");wrap.append(edit);edited=edit;
        wrap.append(button("拒绝这份补丁",async()=>{await api(`/runs/${ticket.run_id}/actions/${action.action_id}/reject`,{payload_hash:action.payload_hash,reason:"用户拒绝"});await show(id);}));
        wrap.append(button("提交编辑并重新测试",async()=>{await api(`/runs/${ticket.run_id}/actions/${action.action_id}/revise`,{payload_hash:action.payload_hash,diff:edit.value});await show(id);}));
      }
    }
    if (action.status === "outcome_unknown") wrap.append(button("只读核对远端结果",async()=>{const result=await api(`/runs/${ticket.run_id}/actions/${action.action_id}/reconcile`,{});notice(result.reconciled ? "已核对完成的远端对象。" : "结果仍未确认，保留现场等待检查。");await show(id);}));
    if (action.status === "proposed") {
      const approve=button(action.action_type === "draft_pr" ? "批准已测试的完整补丁" : "批准这条完整内容",async()=>{await api(`/runs/${ticket.run_id}/approvals`,{action_id:action.action_id,payload_hash:action.payload_hash}); await show(id);});
      if (edited) edited.addEventListener("input",()=>{approve.disabled=edited.value!==action.payload.diff;if(approve.disabled)notice("补丁已编辑，请先提交重新测试。");});
      wrap.append(approve);
    }
    $("actions").append(wrap);
  }
}
$("login-form").addEventListener("submit",async event=>{
  event.preventDefault(); token=$("token").value;
  try { await refresh(); await refreshKnowledge(); $("token").value=""; $("login").hidden=true; $("workspace").hidden=false; $("connection").textContent="本地工作台已连接"; notice(""); timer=setInterval(()=>Promise.all([refresh(),refreshKnowledge()]).catch(error=>notice(error.message)),2500); } catch(error) { token=""; notice(error.message); }
});
$("event-form").addEventListener("submit",async event=>{
  event.preventDefault(); try { const result=await api("/events",{source:"web",intent:"investigate",issue_number:Number($("issue").value),body:$("body").value,client_request_id:crypto.randomUUID()}); $("submitted").textContent=result.route === "direct" ? "补充消息已交给当前任务。" : "请求已接收，工单会在路由后显示。"; $("body").value=""; await refresh(); notice(""); } catch(error) {notice(error.message);}
});
$("refresh").addEventListener("click",()=>refresh().then(()=>selected ? show(selected) : null).catch(error=>notice(error.message)));
window.addEventListener("pagehide",()=>{clearInterval(timer);token="";});

async function refreshKnowledge() {
  const [docs,jobs,status,memories]=await Promise.all([api("/knowledge/documents"),api("/knowledge/jobs"),api("/knowledge/status"),api("/memories")]);
  $("knowledge-status").textContent=`${status.mode === "local" ? "本地语义模型" : "协议演示"} · ${jobs.filter(j=>j.status === "queued" || j.status === "indexing").length} 个资料正在处理`;
  $("knowledge-documents").replaceChildren();
  const seen=new Set();
  for (const doc of docs) {
    if (seen.has(doc.document_id)) continue; seen.add(doc.document_id);
    const row=document.createElement("div"), label=document.createElement("p"); label.textContent=`${doc.title} · ${doc.source_version} · ${doc.status === "revoked" ? "已撤销" : "可用"}`;
    row.append(label,button(doc.status === "revoked" ? "恢复访问" : "撤销访问",async()=>{await api(`/knowledge/documents/${doc.document_id}/acl`,{readers:doc.readers,revoked:doc.status !== "revoked"},"PATCH"); await refreshKnowledge();})); $("knowledge-documents").append(row);
  }
  for (const job of jobs.filter(j=>j.status === "failed").slice(0,3)) {const text=document.createElement("p"); text.textContent=`${job.title}：处理失败（${job.reason_code}）`; $("knowledge-documents").append(text);}
  $("memories").replaceChildren();
  for (const memory of memories) { const row=document.createElement("div"),text=document.createElement("pre");text.textContent=memory.summary; row.append(text,button("撤销摘要",async()=>{await api(`/memories/${memory.id}`,undefined,"DELETE");await refreshKnowledge();})); $("memories").append(row); }
}
$("knowledge-form").addEventListener("submit",async event=>{
  event.preventDefault(); try {
    const file=$("knowledge-file").files[0]; if (!file || file.size>45000) throw new Error("请选择不超过 45 KB 的资料。");
    const ext=file.name.split(".").pop().toLowerCase(),types={md:"text/markdown",txt:"text/plain",py:"text/x-python",pdf:"application/pdf"}; if (!types[ext]) throw new Error("暂不支持该文件类型。");
    const bytes=new Uint8Array(await file.arrayBuffer()); let binary=""; for (const byte of bytes) binary+=String.fromCharCode(byte);
    await api("/knowledge/documents",{title:$("knowledge-title").value,source_uri:$("knowledge-uri").value,source_version:$("knowledge-version").value,media_type:types[ext],content_base64:btoa(binary)}); notice("资料已接收，完成处理后可用于检索。"); await refreshKnowledge();
  } catch(error) {notice(error.message);}
});
$("search-form").addEventListener("submit",async event=>{
  event.preventDefault(); try {
    const result=await api("/knowledge/search",{query:$("search-query").value}); $("search-results").replaceChildren();
    for (const hit of result.hits) { const row=document.createElement("div"), text=document.createElement("pre");text.textContent=`${hit.title} · ${hit.source_version}\n${hit.snippet}`; row.append(text,button("读取原文片段",async()=>{const source=await api(`/knowledge/chunks/${hit.chunk_id}?snapshot_id=${result.snapshot_id}`);text.textContent=`${source.source_uri} · ${source.source_version}\n${source.text}`;}));$("search-results").append(row); }
  } catch(error) {notice(error.message);}
});

$("repair-form").addEventListener("submit",async event=>{
  event.preventDefault();try {const result=await api("/repairs",{project_id:"orders-demo",issue_number:Number($("repair-issue").value),question:"Fix paid revenue: count the first paid order per ID, ignoring other statuses.",client_request_id:crypto.randomUUID(),execution_mode:"fixture"});await refresh();await show(result.ticket_id);notice("修复已接收，测试完成后可审阅完整补丁。");} catch(error){notice(error.message);}
});
