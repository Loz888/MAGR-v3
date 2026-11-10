/* RepoSage V3.4 unified repository workbench. All remote content is rendered as text. */
"use strict";

const $ = (id) => document.getElementById(id);
let selectedTask = null;
let selectedFinding = null;
let reviewedDiff = null;

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
  if (task.pending && task.pending.state) {
    const pending = statusBadge(task.pending.state);
    pending.className = "badge pending " + task.pending.state;
    pending.title = task.pending.reason;
    summary.append(pending);
  }
  const detail = document.createElement("pre");
  detail.className = "output";
  show(detail, {
    repository: task.repository,
    commit: task.commit,
    fixed_snapshot: { commit: task.commit, ref: task.ref || null },
    parent_task_id: task.parent_task_id,
    thread_id: task.thread_id,
    pending: task.pending,
    failure: task.failure,
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
  // Any re-render invalidates the previously reviewed diff: the approval
  // button only re-enables after the reviewer loads the current diff again.
  reviewedDiff = null;
  $("review-meta").textContent = "";
  $("review-diff").textContent = "";
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
    show($("review-meta"), {
      available: true,
      note: "点击“加载完整 Diff”核对完整补丁与 payload_hash 后再审批。",
      files: finding.fix.files,
      diff_sha256: finding.fix.diff_sha256,
      bundle_payload_hash: finding.fix.bundle.payload_hash,
      before_receipt: finding.fix.before_receipt,
      after_receipt: finding.fix.after_receipt,
    });
  } else {
    show($("review-meta"), { available: false, reason: "No verified patch yet." });
  }
  show($("finding-memory"), {
    note: "Memory 仅作参考，命中不会覆盖当前仓库证据；来源与证据 ID 如下。",
    memory: task.memory,
  });
  show($("finding-publication"), {
    approval: finding.approval || { status: "not_requested" },
    publication: finding.publication || { status: "not_started" },
    github_link: finding.pull_request_link,
    link_note: finding.pull_request_link
      ? "Draft PR 可在 GitHub 打开；工作台从不合并。"
      : "没有可点击的 GitHub 链接（fixture 回执或缺少 GitHub 写入授权）。",
    merged: false,
    note: "Approval 绑定 payload_hash；PR Created 只代表 Draft PR 已回执，工作台不会合并。",
  });
  updateFindingButtons();
}

function updateFindingButtons() {
  const finding = selectedFinding;
  if (!finding) return;
  $("reproduce-finding").disabled = !(
    finding.stage === "suspected" && finding.reproduction_plan
  );
  $("repair-finding").disabled = finding.stage !== "reproduced";
  const approval = finding.approval;
  const reviewed =
    reviewedDiff !== null &&
    approval !== null &&
    approval !== undefined &&
    reviewedDiff.payload_hash === approval.payload_hash;
  $("approve-finding").disabled = !(finding.stage === "waiting_approval" && reviewed);
}

async function loadDiff() {
  if (!selectedTask || !selectedFinding) return;
  const result = await call(
    "GET",
    `/api/v3/workbench/tasks/${selectedTask.task_id}/findings/${selectedFinding.finding_id}/diff`
  );
  if (!result.ok) {
    reviewedDiff = null;
    updateFindingButtons();
    show($("review-meta"), result.payload);
    return;
  }
  reviewedDiff = result.payload;
  show($("review-meta"), {
    payload_hash: result.payload.payload_hash,
    diff_sha256: result.payload.diff_sha256,
    files: result.payload.files,
    tree_hash_before: result.payload.tree_hash_before,
    tree_hash_after: result.payload.tree_hash_after,
    before_receipt: result.payload.before_receipt,
    after_receipt: result.payload.after_receipt,
    reviewed: true,
  });
  $("review-diff").textContent = result.payload.diff;
  updateFindingButtons();
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
  show($("usage"), selectedTask.usage);
  show($("failure"), selectedTask.failure || { status: "none", reason: "" });
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

function submissionRequestId() {
  const form = $("create-form");
  if (!form.dataset.requestId) {
    form.dataset.requestId =
      typeof crypto !== "undefined" && crypto.randomUUID
        ? crypto.randomUUID()
        : `web-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }
  return form.dataset.requestId;
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
    client_request_id: submissionRequestId(),
  };
  const submit = $("create-form").querySelector('button[type="submit"]');
  submit.disabled = true;
  try {
    const result = await call("POST", "/api/v3/workbench/tasks", body);
    toast(result, $("create-result"));
    if (result.ok) {
      delete $("create-form").dataset.requestId;
      selectedFinding = null;
      reviewedDiff = null;
      await refreshTasks();
      await openTask(result.payload.task_id);
    }
  } finally {
    submit.disabled = false;
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
    if (!reviewedDiff || !selectedFinding.approval) {
      show($("create-result"), {
        code: "DIFF_REVIEW_REQUIRED",
        reason: "请先加载完整 Diff 核对 payload_hash，再审批。",
      });
      return;
    }
    if (reviewedDiff.payload_hash !== selectedFinding.approval.payload_hash) {
      show($("create-result"), {
        code: "REVIEW_STALE",
        reason: "你审查的 Diff 已不是当前待审批的 Diff，请重新加载。",
      });
      return;
    }
    const approved = window.confirm(
      "确认审批已审查的 Patch 并创建 Draft PR？\n\npayload_hash: " +
        reviewedDiff.payload_hash +
        "\n\n该操作只创建 Draft PR，不会合并。"
    );
    if (!approved) return;
    body = { payload_hash: reviewedDiff.payload_hash };
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
  $("load-diff").addEventListener("click", loadDiff);
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
