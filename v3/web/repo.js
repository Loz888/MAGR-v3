/* RepoSage V3.2 repository analysis workbench. All remote content is rendered as text. */
"use strict";

const $ = (id) => document.getElementById(id);
let selectedRun = null;

function token() {
  return $("token").value.trim();
}

async function call(method, path, body) {
  const headers = { "X-MGAR-Token": token() };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  let payload = text;
  try {
    payload = JSON.parse(text);
  } catch (error) {
    payload = text;
  }
  return { ok: response.ok, status: response.status, payload };
}

function show(element, value) {
  element.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

function statusClass(status) {
  if (status === "succeeded" || status === "internal_report") return "ok";
  if (status === "failed" || status === "cancelled") return "bad";
  return "pending";
}

async function loadProfile() {
  const result = await call("GET", "/api/v3/repository-analyses/profile");
  show($("profile"), result.payload);
}

async function loadRuns() {
  const result = await call("GET", "/api/v3/repository-analyses");
  const container = $("runs");
  container.textContent = "";
  if (!result.ok) {
    show($("create-result"), result.payload);
    return;
  }
  for (const run of result.payload) {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "run";
    const title = document.createElement("strong");
    title.textContent = run.repository + "@" + run.commit.slice(0, 12);
    const meta = document.createElement("span");
    meta.className = "run-meta " + statusClass(run.run_status);
    meta.textContent =
      run.run_status +
      " · " +
      run.execution_mode +
      " · 模型 " +
      run.model_calls_used +
      "/" +
      run.limits.max_model_calls +
      " · 工具 " +
      run.tool_calls_used +
      "/" +
      run.limits.max_tool_calls +
      (run.stop_reason ? " · " + run.stop_reason : "");
    const task = document.createElement("span");
    task.className = "run-task";
    task.textContent = run.task;
    card.append(title, meta, task);
    card.addEventListener("click", () => selectRun(run.run_id));
    container.append(card);
  }
}

async function selectRun(runId) {
  selectedRun = runId;
  $("detail-card").hidden = false;
  const detail = await call("GET", "/api/v3/repository-analyses/" + runId);
  if (!detail.ok) {
    show($("detail"), detail.payload);
    return;
  }
  show($("detail"), detail.payload);
  const report = await call("GET", "/api/v3/repository-analyses/" + runId + "/report");
  show($("report"), report.ok ? report.payload : report.payload);
}

async function submitCreate(event) {
  event.preventDefault();
  const limits = {
    max_model_calls: Number($("model-calls").value),
    max_tool_calls: Number($("tool-calls").value),
    max_input_tokens: Number($("input-tokens").value),
    max_cost_micros: Number($("cost-micros").value),
    max_seconds: Number($("seconds").value),
  };
  const body = {
    repository_url: $("repo-url").value.trim(),
    task: $("task").value.trim(),
    execution_mode: $("execution-mode").value,
    client_request_id: crypto.randomUUID(),
    limits,
  };
  if ($("ref").value.trim()) body.ref = $("ref").value.trim();
  if ($("commit").value.trim()) body.commit = $("commit").value.trim();
  const result = await call("POST", "/api/v3/repository-analyses", body);
  show($("create-result"), result.payload);
  if (result.ok) {
    await loadRuns();
    await selectRun(result.payload.run_id);
  }
}

async function sendFollowUp() {
  if (!selectedRun) return;
  const body = $("follow-up").value.trim();
  if (!body) return;
  const result = await call(
    "POST",
    "/api/v3/repository-analyses/" + selectedRun + "/messages",
    { client_request_id: crypto.randomUUID(), body }
  );
  show($("follow-up-result"), result.payload);
  if (result.ok) {
    $("follow-up").value = "";
    await loadRuns();
  }
}

async function control(action) {
  if (!selectedRun) return;
  const result = await call(
    "POST",
    "/api/v3/repository-analyses/" + selectedRun + "/" + action
  );
  show($("detail"), result.payload);
  await loadRuns();
  await selectRun(selectedRun);
}

$("save-token").addEventListener("click", () => {
  sessionStorage.setItem("mgar-token", token());
  show($("profile"), { token_saved: true });
});
$("load-profile").addEventListener("click", loadProfile);
$("refresh").addEventListener("click", loadRuns);
$("create-form").addEventListener("submit", submitCreate);
$("send-follow-up").addEventListener("click", sendFollowUp);
$("pause").addEventListener("click", () => control("pause"));
$("resume").addEventListener("click", () => control("resume"));
$("cancel").addEventListener("click", () => control("cancel"));

$("token").value = sessionStorage.getItem("mgar-token") || "";
loadProfile();
loadRuns();
