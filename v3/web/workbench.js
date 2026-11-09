/* RepoSage V3.4 unified repository workbench. All remote content is rendered as text. */
"use strict";

const $ = (id) => document.getElementById(id);
let selectedTask = null;
let selectedFinding = null;

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

function badge(stage, label) {
  const span = document.createElement("span");
  span.className = "badge " + stage;
  span.textContent = label || stage;
  return span;
}

function statusBadge(status) {
  const span = document.createElement("span");
  span.className = "badge status";
  span.textContent = status;
  return span;
}

function textCell(value) {
  const td = document.createElement("td");
  td.textContent = value === null || value === undefined ? "" : String(value);
  return td;
}

function splitLines(value) {
  return value
    .split(/\r?\n/)
    .map((item) => item.trim())
    .filter((item) => item.length > 0);
}

function toast(result, element) {
  show(element, result.payload);
}

async function loadProfile() {
  if (!token()) return;
  const result = await call("GET", "/api/v3/workbench/profile");
  show($("profile"), result.payload);
}

async function refreshTasks() {
  if (!token()) return;
  const result = await call("GET", "/api/v3/workbench/tasks");
  const runs = $("runs");
  runs.replaceChildren();
  if (!result.ok) {
    const line = document.createElement("pre");
    line.className = "output";
    show(line, result.payload);
    runs.append(line);
    return;
  }
  for (const task of result.payload) {
    const button = document.createElement("button");
    button.className = "run";
    const title = document.createElement("div");
    title.className = "run-title";
    title.textContent = `[${task.mode}] ${task.repository}@${task.commit.slice(0, 12)} · ${task.stage_label}`;
    const meta = document.createElement("div");
    meta.className = "run-meta";
    meta.textContent = `${task.status} · ${task.findings} finding(s) · ${task.execution_mode} · ${task.task_id}`;
    button.append(title, meta);
    button.addEventListener("click", () => openTask(task.task_id));
    runs.append(button);
  }
  if (result.payload.length === 0) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "还没有任务。创建 Audit 或 Issue Repair 开始。";
    runs.append(empty);
  }
}

function renderSummary(task) {
  const summary = $("task-summary");
  summary.replaceChildren(
    badge(task.stage, task.stage_label),
    statusBadge(task.status),
    statusBadge(task.mode),
    statusBadge(task.execution_mode)
  );
  const detail = document.createElement("pre");
  detail.className = "output";
  show(detail, {
    repository: task.repository,
    commit: task.commit,
    parent_task_id: task.parent_task_id,
    thread_id: task.thread_id,
    next_action: task.next_action,
    controls: task.controls,
    summary: task.summary,
  });
  summary.append(detail);
}

function renderTimeline(task) {
  const list = $("timeline");
  list.replaceChildren();
  for (const entry of task.timeline) {
    const li = document.createElement("li");
    const label = document.createElement("strong");
    label.textContent = `${entry.stage}/${entry.status}`;
    const text = document.createElement("span");
    text.textContent = " — " + entry.summary;
    const when = document.createElement("span");
    when.className = "when";
    when.textContent = entry.created_at;
    li.append(label, text, when);
    list.append(li);
  }
}

function renderFindings(task) {
  const table = $("findings");
  const body = table.querySelector("tbody");
  body.replaceChildren();
  $("finding-disclaimer").textContent =
    "Suspected ≠ 已修复。建议修复只是提案；只有 Patch Verified 之后才可审批，PR Created 也只表示 Draft PR 已回执，从不合并。";
  for (const finding of task.findings) {
    const row = document.createElement("tr");
    row.dataset.findingId = finding.finding_id;
    if (selectedFinding && selectedFinding.finding_id === finding.finding_id) {
      row.className = "selected";
    }
    const rank = textCell(finding.risk_rank);
    const stage = document.createElement("td");
    stage.append(badge(finding.stage, finding.label));
    const severity = textCell(finding.severity);
    const location = textCell(
      finding.path ? `${finding.path}:${finding.start_line}-${finding.end_line}` : ""
    );
    const title = textCell(finding.title);
    const next = document.createElement("td");
    next.className = "finding-next";
    next.textContent =
      finding.stage === "suspected" && finding.reproduction_plan
        ? "Reproduce"
        : finding.stage === "reproduced"
          ? "Prepare Repair"
          : finding.stage === "waiting_approval"
            ? "Approve"
            : "—";
    row.append(rank, stage, severity, location, title, next);
    row.addEventListener("click", () => openFinding(task.task_id, finding.finding_id));
    body.append(row);
  }
  if (task.findings.length === 0) {
    const row = document.createElement("tr");
    const cell = textCell("没有 finding。请检查 Agent 执行时间线与扫描覆盖范围。");
    cell.colSpan = 6;
    row.append(cell);
    body.append(row);
  }
}

function renderFinding(task, finding) {
  selectedFinding = finding;
  $("finding-card").hidden = false;
  const summary = $("finding-summary");
  summary.replaceChildren(
    badge(finding.stage, finding.label),
    statusBadge(`fix: ${finding.fix_state}`),
    statusBadge(`risk ${finding.risk_score}`)
  );
  const detail = document.createElement("pre");
  detail.className = "output";
  show(detail, {
    finding_id: finding.finding_id,
    title: finding.title,
    summary: finding.summary,
    severity: finding.severity,
    classification: finding.classification,
    confidence: finding.confidence,
    suggested_fix: finding.suggested_fix,
    suggested_fix_is_not_applied: finding.suggested_fix_is_not_applied,
    disclaimer: finding.disclaimer,
  });
  summary.append(detail);
  show($("finding-source"), finding.source);
  show($("finding-reproduction"), finding.reproduction || { status: "not_attempted" });
  if (finding.fix) {
    show($("finding-diff"), {
      diff: finding.fix.diff,
      diff_sha256: finding.fix.diff_sha256,
      files: finding.fix.files,
      tree_hash_before: finding.fix.tree_hash_before,
      tree_hash_after: finding.fix.tree_hash_after,
      before_receipt: finding.fix.before_receipt,
      after_receipt: finding.fix.after_receipt,
      bundle_payload_hash: finding.fix.bundle.payload_hash,
    });
  } else {
    show($("finding-diff"), { available: false, reason: "No verified patch yet." });
  }
  show($("finding-memory"), {
    note: "Memory 仅作参考，命中不会覆盖当前仓库证据；来源与证据 ID 如下。",
    memory: task.memory,
  });
  show($("finding-publication"), {
    approval: finding.approval || { status: "not_requested" },
    publication: finding.publication || { status: "not_started" },
    merged: false,
    note: "Approval 绑定 payload_hash；PR Created 只代表 Draft PR 已回执，工作台不会合并。",
  });
  $("reproduce-finding").disabled = !(finding.stage === "suspected" && finding.reproduction_plan);
  $("repair-finding").disabled = finding.stage !== "reproduced";
  $("approve-finding").disabled = !(finding.stage === "waiting_approval" && finding.approval);
}

function renderControls(task) {
  const controls = task.controls;
  $("run-task").disabled = !controls.can_run;
  $("pause-task").disabled = !controls.can_pause;
  $("resume-task").disabled = !controls.can_resume;
  $("cancel-task").disabled = !controls.can_cancel;
}

async function openTask(taskId) {
  const result = await call("GET", "/api/v3/workbench/tasks/" + taskId);
  if (!result.ok) {
    show($("create-result"), result.payload);
    return;
  }
  selectedTask = result.payload;
  $("detail-card").hidden = false;
  $("findings-card").hidden = false;
  $("follow-up-card").hidden = false;
  renderSummary(selectedTask);
  $("coverage").textContent = JSON.stringify(selectedTask.coverage, null, 2);
  renderTimeline(selectedTask);
  renderFindings(selectedTask);
  renderControls(selectedTask);
  if (selectedFinding) {
    const fresh = selectedTask.findings.find(
      (item) => item.finding_id === selectedFinding.finding_id
    );
    if (fresh) renderFinding(selectedTask, fresh);
  }
}

async function openFinding(taskId, findingId) {
  const result = await call(
    "GET",
    `/api/v3/workbench/tasks/${taskId}/findings/${findingId}`
  );
  if (!result.ok) {
    show($("create-result"), result.payload);
    return;
  }
  renderFinding(selectedTask, result.payload);
  for (const row of $("findings").querySelectorAll("tbody tr")) {
    row.classList.toggle("selected", row.dataset.findingId === findingId);
  }
}

async function createTask(event) {
  event.preventDefault();
  const include = splitLines($("include-paths").value);
  const excludes = splitLines($("exclude-paths").value);
  const mode = $("mode").value;
  const body = {
    repository_url: $("repo-url").value.trim(),
    mode,
    task: $("task").value.trim(),
    ref: $("ref").value.trim() || null,
    commit: $("commit").value.trim() || null,
    issue_number: $("issue-number").value ? Number($("issue-number").value) : null,
    execution_mode: $("execution-mode").value,
    coverage: {
      scope: include.length > 0 ? "paths" : "repository",
      include_paths: include,
      exclude_paths: excludes,
    },
    auto_start: $("auto-start").checked,
  };
  const result = await call("POST", "/api/v3/workbench/tasks", body);
  toast(result, $("create-result"));
  if (result.ok) {
    selectedFinding = null;
    await refreshTasks();
    await openTask(result.payload.task_id);
  }
}

async function taskAction(control) {
  if (!selectedTask) return;
  const result = await call(
    "POST",
    `/api/v3/workbench/tasks/${selectedTask.task_id}/${control}`
  );
  toast(result, $("create-result"));
  if (result.ok) await openTask(selectedTask.task_id);
}

async function findingAction(action) {
  if (!selectedTask || !selectedFinding) return;
  let body;
  if (action === "approve") {
    const approved = window.confirm(
      "确认审批该 Patch 并创建 Draft PR？\n\npayload_hash: " +
        selectedFinding.approval.payload_hash +
        "\n\n该操作只创建 Draft PR，不会合并。"
    );
    if (!approved) return;
    body = { payload_hash: selectedFinding.approval.payload_hash };
  }
  const result = await call(
    "POST",
    `/api/v3/workbench/tasks/${selectedTask.task_id}/findings/${selectedFinding.finding_id}/${action}`,
    body
  );
  toast(result, $("create-result"));
  if (result.ok) await openTask(selectedTask.task_id);
}

async function sendFollowUp() {
  if (!selectedTask) return;
  const body = $("follow-up").value.trim();
  if (!body) return;
  const result = await call(
    "POST",
    `/api/v3/workbench/tasks/${selectedTask.task_id}/messages`,
    { body }
  );
  toast(result, $("follow-up-result"));
  if (result.ok) {
    $("follow-up").value = "";
    selectedFinding = null;
    await refreshTasks();
    await openTask(result.payload.task_id);
  }
}

function boot() {
  $("token").value = sessionStorage.getItem("mgar-token") || "";
  $("save-token").addEventListener("click", () => {
    sessionStorage.setItem("mgar-token", token());
    loadProfile();
    refreshTasks();
  });
  $("load-profile").addEventListener("click", loadProfile);
  $("refresh").addEventListener("click", refreshTasks);
  $("create-form").addEventListener("submit", createTask);
  $("run-task").addEventListener("click", () => taskAction("run"));
  $("pause-task").addEventListener("click", () => taskAction("pause"));
  $("resume-task").addEventListener("click", () => taskAction("resume"));
  $("cancel-task").addEventListener("click", () => taskAction("cancel"));
  $("reproduce-finding").addEventListener("click", () => findingAction("reproduce"));
  $("repair-finding").addEventListener("click", () => findingAction("repair"));
  $("approve-finding").addEventListener("click", () => findingAction("approve"));
  $("send-follow-up").addEventListener("click", sendFollowUp);
  $("mode").addEventListener("change", () => {
    $("issue-number").disabled = $("mode").value !== "issue_repair";
  });
  if (token()) {
    loadProfile();
    refreshTasks();
  }
}

boot();
