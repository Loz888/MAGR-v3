let token = "", selected = null, timer = null;
const $ = id => document.getElementById(id);
const labels = {queued:"等待执行",running:"调查中",waiting_approval:"等待审批",waiting_input:"等待补充",paused:"已暂停",succeeded:"已完成",failed:"执行失败",cancelled:"已取消"};
async function api(path, body) {
  const response = await fetch("/api/v3" + path, {method: body === undefined ? "GET" : "POST", headers:{"X-MGAR-Token":token,"Content-Type":"application/json"},body: body === undefined ? undefined : JSON.stringify(body)});
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
    const controls=ticket.graph_version === "r2-mailbox-v1" ? [ticket.stage === "paused" ? ["恢复","resume"] : ["暂停","pause"],["取消","cancel"]] : [["取消","cancel"]];
    for (const [label,action] of controls) $("detail-controls").append(button(label,async()=>{await api(`/tickets/${id}/${action}`,{}); await refresh(); await show(id);}));
  }
  $("related").replaceChildren();
  for (const related of ticket.related_tickets) $("related").append(button(related.question,()=>show(related.ticket_id)));
  if (!ticket.related_tickets.length) $("related").textContent="暂无关联工单";
  $("events").textContent=await api(`/runs/${ticket.run_id}/events`);
  const actions=await api(`/runs/${ticket.run_id}/actions`); $("actions").replaceChildren();
  for (const action of actions) {
    const wrap=document.createElement("div"); wrap.className="action";
    const state=document.createElement("p"); state.textContent=`${action.reply_kind === "system" ? "系统回复" : "调查结果"} · ${action.status}`;
    const preview=document.createElement("pre"); preview.textContent=action.payload.body; wrap.append(state,preview);
    if (action.status === "proposed") wrap.append(button("批准这条完整内容",async()=>{await api(`/runs/${ticket.run_id}/approvals`,{action_id:action.action_id,payload_hash:action.payload_hash}); await show(id);}));
    $("actions").append(wrap);
  }
}
$("login-form").addEventListener("submit",async event=>{
  event.preventDefault(); token=$("token").value;
  try { await refresh(); $("token").value=""; $("login").hidden=true; $("workspace").hidden=false; $("connection").textContent="本地工作台已连接"; notice(""); timer=setInterval(()=>refresh().catch(error=>notice(error.message)),2500); } catch(error) { token=""; notice(error.message); }
});
$("event-form").addEventListener("submit",async event=>{
  event.preventDefault(); try { const result=await api("/events",{source:"web",intent:"investigate",issue_number:Number($("issue").value),body:$("body").value,client_request_id:crypto.randomUUID()}); $("submitted").textContent=result.route === "direct" ? "补充消息已交给当前任务。" : "请求已接收，工单会在路由后显示。"; $("body").value=""; await refresh(); notice(""); } catch(error) {notice(error.message);}
});
$("refresh").addEventListener("click",()=>refresh().then(()=>selected ? show(selected) : null).catch(error=>notice(error.message)));
window.addEventListener("pagehide",()=>{clearInterval(timer);token="";});
