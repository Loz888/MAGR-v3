/* The UI is a client of the durable repository agent, never a simulated answer generator. */
"use strict";

const $ = (id) => document.getElementById(id);
const API = "/api/v3/repository-analyses";
const ACTIVE = new Set(["queued", "running"]);
const LABELS = {
  queued: "等待执行",
  running: "正在分析",
  succeeded: "分析完成",
  failed: "分析失败",
  paused: "已暂停",
  cancelled: "已取消",
  waiting_input: "需要补充信息",
};
const TOOLS = {
  repository_metadata: "查看仓库",
  list_directory: "浏览目录",
  read_file: "阅读源码",
  search_code: "搜索代码",
  read_issue: "阅读 Issue",
  read_pull_request: "阅读 PR",
};
const ERRORS = {
  AUTH_REQUIRED: "访问令牌无效，请在连接设置中重新输入。",
  ORIGIN_DENIED:
    "当前访问地址未获服务器允许，请使用 http://127.0.0.1:18080 访问。",
  MODEL_CREDENTIAL_REQUIRED:
    "服务器尚未配置 DeepSeek 密钥。请在设置中查看配置步骤。",
  REPO_ANALYSIS_PROFILE_REQUIRED:
    "服务器尚未配置真实分析授权，请在设置中查看配置步骤。",
  REPO_ANALYSIS_PROFILE_INVALID:
    "服务器的分析授权无效，请检查授权人、预算和配置文件。",
  REPO_ANALYSIS_SCOPE_DENIED:
    "该仓库或任务预算不在当前授权范围内，请核对服务器的分析授权。",
  GITHUB_READ_CREDENTIAL_REQUIRED: "服务器尚未配置 GitHub 只读凭据。",
  GITHUB_NOT_FOUND: "没有找到仓库或指定文件，请检查 URL、分支和读取权限。",
  REPOSITORY_NOT_PUBLIC: "当前仅支持公开的 GitHub 仓库。",
  GITHUB_FORBIDDEN: "GitHub 拒绝了读取请求，请检查服务器上的只读凭据权限。",
  GITHUB_RATE_LIMITED: "GitHub 请求额度暂时用尽，请稍后再试。",
  REF_NOT_FOUND: "没有找到指定分支、Tag 或 Commit，请核对仓库版本。",
  BUDGET_EXHAUSTED: "本次模型或费用预算已用尽，以下保留已获得的结果。",
  INPUT_TOKEN_BUDGET: "本次上下文预算已用尽，可以缩小问题范围后继续追问。",
  DEADLINE_EXCEEDED: "本次分析已达到授权的时间上限，以下保留已获得的结果。",
  USAGE_UNKNOWN: "存在尚未核实的模型用量，需要操作员核账后才能恢复。",
  MODEL_PROVIDER_ERROR:
    "模型服务返回错误，请检查密钥、模型名称和供应商账户额度。",
  MODEL_INVALID_RESPONSE:
    "模型输出没有通过结构校验，本次尝试已保留，请查看执行记录。",
  EXECUTION_FAILED: "执行失败。请查看服务器日志，并保留本次运行记录以便排查。",
  REPORT_VERIFICATION_FAILED:
    "生成的报告没有通过源码引用核验，不能作为成功结论。",
  INVALID_INPUT: "输入格式有误，请检查 GitHub 仓库地址、问题和分支。",
  REQUEST_ID_REUSED: "该请求标识已被其他内容使用，请新建一次请求。",
};

function saved(key) {
  try {
    return sessionStorage.getItem(key) || "";
  } catch {
    return "";
  }
}
function save(key, value) {
  try {
    if (value) sessionStorage.setItem(key, value);
    else sessionStorage.removeItem(key);
  } catch {
    /* Memory-only use remains possible. */
  }
}
const state = {
  token: saved("mgar-token"),
  profile: null,
  runs: [],
  selected: null,
  details: new Map(),
  demo: saved("mgar-demo") === "1",
  busy: false,
  polling: false,
  selection: 0,
  signature: "",
  request: null,
  timer: null,
  connected: false,
};
function isDemo() {
  const run = state.details.get(state.selected)?.run;
  return run ? run.execution_mode === "fixture" : state.demo;
}
function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function notify(message, error = false) {
  $("notice").textContent = message;
  $("notice").classList.toggle("error", error);
  $("notice").hidden = !message;
}
function explain(error) {
  return ERRORS[error.code] || error.message || "请求未完成，请检查服务连接。";
}
async function call(method, path, body) {
  const response = await fetch(path, {
    method,
    headers: {
      "X-MGAR-Token": state.token,
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  let data;
  try {
    data = JSON.parse(text);
  } catch {
    data = text;
  }
  if (!response.ok) {
    const error = new Error(
      typeof data === "object"
        ? data.message
        : "服务器返回 HTTP " + response.status,
    );
    error.code = data && data.code;
    error.status = response.status;
    if (response.status === 401) {
      state.connected = false;
      $("connection-label").textContent = "请重新连接";
      $("connection-dot").className = "status-dot error";
    }
    throw error;
  }
  return data;
}
function showSettings() {
  if (!$("settings-dialog").open) $("settings-dialog").showModal();
  $("access-token").focus();
}
function renderProfile() {
  const profile = state.profile;
  $("readiness").hidden = !profile;
  $("checklist").replaceChildren();
  $("setup-help").replaceChildren();
  if (!profile) return;
  const names = {
    paid_enabled: "服务器已启用真实模型",
    model_configured: "DeepSeek 密钥已配置",
    github_configured: "GitHub 只读凭据已配置",
    authorization_valid: "分析授权有效",
  };
  for (const [key, label] of Object.entries(names)) {
    const ok = Boolean(profile.checks && profile.checks[key]);
    const item = element("li");
    item.append(
      element("span", ok ? "pass" : "missing", ok ? "✓" : "○"),
      element("span", "", label),
    );
    $("checklist").append(item);
  }
  for (const issue of profile.issues || []) {
    const item = element("div", "setup-item");
    item.append(
      element("strong", "", issue.message),
      element("span", "", issue.action),
    );
    $("setup-help").append(item);
  }
  $("profile-summary").textContent = profile.model
    ? "模型：" +
      profile.model.model_id +
      " · 每次分析最多 " +
      profile.limits.max_model_calls +
      " 轮 · 费用上限 $" +
      (profile.limits.max_cost_micros / 1000000).toFixed(2) +
      " · 授权仓库：" +
      profile.allowed_repositories.join("、")
    : "配置保存在服务器端。修改 .env 后请重启服务，再点击上方按钮重新检查。";
  $("model-label").textContent = profile.model
    ? profile.model.model_id
    : "REPOSITORY AGENT";
  $("connection-label").textContent = profile.ready
    ? "已连接 · 模型就绪"
    : "已连接 · 待配置";
  $("connection-dot").className =
    "status-dot " + (profile.ready ? "ready" : "error");
}
function updateMode() {
  $("mode-badge").textContent = isDemo()
    ? "离线演示 · 合成样例"
    : "真实仓库分析";
  $("mode-badge").classList.toggle("demo", isDemo());
  $("composer-hint").textContent = isDemo()
    ? "演示不会读取所填仓库，也不会调用模型"
    : "真实读取 GitHub · 使用服务器已授权的模型与预算";
  $("demo-mode").checked = state.demo;
}
async function connect(event) {
  if (event) event.preventDefault();
  const token = $("access-token").value.trim() || state.token;
  if (!token) {
    $("connection-error").textContent = "请输入本地访问令牌。";
    return;
  }
  state.token = token;
  $("connect").disabled = true;
  $("connection-error").textContent = "";
  try {
    state.profile = await call("GET", API + "/profile");
    state.connected = true;
    save("mgar-token", state.token);
    $("access-token").value = "";
    renderProfile();
    await loadHistory();
    notify(
      state.profile.ready || state.demo
        ? ""
        : "真实分析还需要完成服务端配置。点击左下角连接设置查看具体步骤。",
    );
    if (state.profile.ready || state.demo) $("settings-dialog").close();
  } catch (error) {
    $("connection-error").textContent = explain(error);
  } finally {
    $("connect").disabled = false;
  }
}
function renderHistory() {
  const selected = state.runs.find((run) => run.run_id === state.selected);
  const threads = new Map();
  for (const run of state.runs)
    if (!threads.has(run.thread_id)) threads.set(run.thread_id, run);
  const host = $("history");
  host.replaceChildren();
  for (const run of threads.values()) {
    const button = element(
      "button",
      "history-item" +
        (selected && selected.thread_id === run.thread_id ? " active" : ""),
    );
    button.type = "button";
    button.title = run.task;
    button.append(
      element("strong", "", run.task),
      element(
        "small",
        "",
        run.repository + " · " + (LABELS[run.run_status] || run.run_status),
      ),
    );
    button.addEventListener("click", () =>
      selectRun(run.run_id).catch((error) => notify(explain(error), true)),
    );
    host.append(button);
  }
  if (!threads.size)
    host.append(
      element("p", "sidebar-empty", "还没有分析记录，提交第一个问题开始。"),
    );
}
async function loadHistory() {
  if (!state.connected) return;
  state.runs = await call("GET", API + "?limit=100");
  renderHistory();
}
function newChat() {
  state.selection++;
  state.selected = null;
  state.signature = "";
  state.request = null;
  save("mgar-selected", "");
  $("welcome").hidden = false;
  $("conversation").hidden = true;
  $("repository-url").disabled = false;
  $("repository-ref").disabled = false;
  $("repository-ref").value = "";
  $("question").value = "";
  $("question").placeholder =
    "你想了解这个项目的什么？描述问题，也可以贴入错误信息…";
  $("send-label").textContent = "开始分析";
  $("page-title").textContent = "新的仓库分析";
  $("sidebar").classList.remove("open");
  renderHistory();
  updateComposer();
  updateMode();
  $("repository-url").focus();
}
function updateComposer() {
  const run = state.details.get(state.selected)?.run;
  const active = run && ACTIVE.has(run.run_status);
  $("send").disabled = state.busy || Boolean(active);
  $("question").disabled = state.busy || Boolean(active);
  $("question").maxLength = 8000;
  $("new-chat").disabled = state.busy;
  for (const button of $("history").querySelectorAll("button"))
    button.disabled = state.busy;
  $("send-label").textContent = state.busy
    ? "正在提交…"
    : active
      ? "正在分析…"
      : state.selected
        ? "继续追问"
        : "开始分析";
  if (run) {
    $("repository-url").value = "https://github.com/" + run.repository;
    $("repository-url").disabled = true;
    $("repository-ref").value = run.commit;
    $("repository-ref").disabled = true;
    $("question").placeholder = active
      ? "Agent 正在阅读和分析代码，你可以在完成后继续追问。"
      : "继续追问这个仓库，或补充错误信息…";
    $("page-title").textContent = run.repository;
  }
}
async function selectRun(id) {
  const version = ++state.selection;
  const detail = await call("GET", API + "/" + encodeURIComponent(id));
  if (version !== state.selection) return;
  state.selected = id;
  state.details.set(id, detail);
  const ancestors = [];
  let current = detail.run;
  const seen = new Set([id]);
  while (
    current.parent_run_id &&
    ancestors.length < 30 &&
    !seen.has(current.parent_run_id)
  ) {
    seen.add(current.parent_run_id);
    const parent =
      state.details.get(current.parent_run_id) ||
      (await call(
        "GET",
        API + "/" + encodeURIComponent(current.parent_run_id),
      ));
    ancestors.push(parent);
    current = parent.run;
  }
  if (version !== state.selection) return;
  for (const parent of ancestors) state.details.set(parent.run.run_id, parent);
  save("mgar-selected", id);
  state.signature = "";
  $("welcome").hidden = true;
  $("conversation").hidden = false;
  $("sidebar").classList.remove("open");
  updateComposer();
  updateMode();
  renderHistory();
  renderConversation();
}
// Build every node as text. Model/repository HTML and unsafe Markdown URLs are never executed.
function inline(parent, text) {
  const pattern =
    /(\x60[^\x60]+\x60|\*\*[^*]+\*\*|\[[^\]]+\]\(https:\/\/[^)\s]+\))/g;
  let offset = 0;
  for (const match of text.matchAll(pattern)) {
    parent.append(document.createTextNode(text.slice(offset, match.index)));
    const value = match[0];
    if (value.charCodeAt(0) === 96)
      parent.append(element("code", "", value.slice(1, -1)));
    else if (value.startsWith("**"))
      parent.append(element("strong", "", value.slice(2, -2)));
    else {
      const parts = value.match(/^\[([^\]]+)\]\((.+)\)$/);
      try {
        const url = new URL(parts[2]);
        if (
          url.protocol !== "https:" ||
          url.hostname !== "github.com" ||
          url.username ||
          url.password ||
          url.port
        )
          throw new Error("Untrusted URL");
        const link = element("a", "", parts[1]);
        link.href = url.href;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        parent.append(link);
      } catch {
        parent.append(document.createTextNode(value));
      }
    }
    offset = match.index + value.length;
  }
  parent.append(document.createTextNode(text.slice(offset)));
}
function markdown(text) {
  const host = element("div");
  let fence = null,
    paragraph = [],
    list = null;
  const flush = () => {
    if (paragraph.length) {
      const p = element("p");
      inline(p, paragraph.join("\n"));
      host.append(p);
      paragraph = [];
    }
    list = null;
  };
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (/^(\x60){3}/.test(line)) {
      if (fence) {
        host.append(fence);
        fence = null;
      } else {
        flush();
        fence = element("pre");
        fence.append(element("code"));
      }
      continue;
    }
    if (fence) {
      fence.firstChild.append(
        document.createTextNode(raw.replace(/^ {4}/, "") + "\n"),
      );
      continue;
    }
    if (!line) {
      flush();
      continue;
    }
    const heading = line.match(/^(#{1,3})\s+(.+)/);
    if (heading) {
      flush();
      const node = element("h" + heading[1].length);
      inline(node, heading[2]);
      host.append(node);
      continue;
    }
    if (/^---+$/.test(line)) {
      flush();
      host.append(element("hr"));
      continue;
    }
    if (line.startsWith("> ")) {
      flush();
      const quote = element("blockquote");
      inline(quote, line.slice(2));
      host.append(quote);
      continue;
    }
    if (/^[-*] /.test(line)) {
      if (paragraph.length) flush();
      if (!list) {
        list = element("ul");
        host.append(list);
      }
      const item = element("li");
      inline(item, line.slice(2));
      list.append(item);
      continue;
    }
    if (list) list = null;
    paragraph.push(raw);
  }
  flush();
  if (fence) host.append(fence);
  return host;
}
function renderTurn(detail) {
  const run = detail.run;
  const article = element("article", "turn");
  article.dataset.runId = run.run_id;
  article.append(element("div", "user-message", run.task));
  const heading = element("div", "assistant-heading");
  heading.append(
    element("span", "assistant-icon", "R"),
    element("span", "", "RepoSage"),
    element(
      "span",
      "run-status " + run.run_status,
      LABELS[run.run_status] || run.run_status,
    ),
  );
  article.append(
    heading,
    element(
      "div",
      "run-meta",
      (run.execution_mode === "fixture"
        ? "合成样例演示"
        : "固定版本 " + run.commit.slice(0, 12)) +
        " · 模型 " +
        run.model_calls_used +
        "/" +
        run.limits.max_model_calls +
        " 轮 · 工具 " +
        run.tool_calls_used +
        "/" +
        run.limits.max_tool_calls +
        " 次",
    ),
  );
  const activity = element("details", "activity");
  activity.dataset.activity = run.run_id;
  const calls = detail.tool_calls || [];
  activity.append(
    element("summary", "", "执行过程 · " + calls.length + " 次工具调用"),
  );
  const log = element("ol");
  if (!calls.length)
    log.append(element("li", "", "Agent 将根据问题选择搜索、读取目录或源码。"));
  for (const call of calls) {
    const row = element("li");
    row.append(
      element("span", "tool-name", TOOLS[call.tool] || call.tool),
      element("span", "", call.result_uri || call.error_code || "正在读取…"),
      element("span", "tool-status", call.status),
    );
    log.append(row);
  }
  activity.append(log);
  article.append(activity);
  const result = element("div", "run-result");
  const reason = run.stop_reason || run.run_reason_code;
  if (reason && !["queued", "running", "paused"].includes(run.run_status)) {
    result.append(
      element(
        "div",
        "run-error",
        (ERRORS[reason] || "本次分析已停止，请保留运行记录并查看服务器日志。") +
          "（" +
          reason +
          "）",
      ),
    );
  }
  if (detail.report) result.append(markdown(detail.report));
  else
    result.append(
      element(
        "p",
        "empty-report",
        ACTIVE.has(run.run_status)
          ? "正在调查代码，完成后结论会自动显示在这里。"
          : run.run_status === "paused"
            ? "分析已暂停，可以恢复或补充问题。"
            : "本次运行还没有生成报告。",
      ),
    );
  if (detail.claims?.length) {
    const evidence = element("details", "evidence-list");
    evidence.append(
      element(
        "summary",
        "",
        "查看 " + detail.claims.length + " 条已核验的源码引用",
      ),
    );
    for (const claim of detail.claims) {
      const block = element("div", "evidence-claim");
      block.append(
        element("strong", "", claim.statement),
        element("blockquote", "", claim.quote),
        element(
          "small",
          "",
          "行 " +
            claim.start_line +
            "–" +
            claim.end_line +
            " · 证据 " +
            claim.evidence_id,
        ),
      );
      evidence.append(block);
    }
    result.append(evidence);
  }
  article.append(result);
  const controls = element("div", "run-controls");
  const actions = ACTIVE.has(run.run_status)
    ? ["pause", "cancel"]
    : run.run_status === "paused"
      ? ["resume", "cancel"]
      : [];
  for (const action of actions) {
    const button = element(
      "button",
      "",
      { pause: "暂停", resume: "恢复", cancel: "取消" }[action],
    );
    button.addEventListener("click", () => control(run.run_id, action, button));
    controls.append(button);
  }
  if (detail.report) {
    const button = element("button", "", "下载报告");
    button.addEventListener("click", () => {
      const url = URL.createObjectURL(
        new Blob([detail.report], { type: "text/markdown;charset=utf-8" }),
      );
      const link = element("a");
      link.href = url;
      link.download = "reposage-" + run.run_id + ".md";
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    });
    controls.append(button);
  }
  article.append(controls);
  return article;
}
function renderConversation() {
  if (!state.selected) return;
  const chain = [],
    seen = new Set();
  let detail = state.details.get(state.selected);
  while (detail && !seen.has(detail.run.run_id)) {
    chain.unshift(detail);
    seen.add(detail.run.run_id);
    detail = state.details.get(detail.run.parent_run_id);
  }
  const signature = JSON.stringify(chain);
  if (signature === state.signature) return;
  state.signature = signature;
  const nearBottom =
    window.scrollY + window.innerHeight >=
    document.documentElement.scrollHeight - 160;
  const opened = new Set(
    [...document.querySelectorAll("details[data-activity][open]")].map(
      (item) => item.dataset.activity,
    ),
  );
  $("conversation").replaceChildren(...chain.map(renderTurn));
  for (const node of document.querySelectorAll("details[data-activity]"))
    if (opened.has(node.dataset.activity)) node.open = true;
  if (nearBottom)
    window.scrollTo({
      top: document.documentElement.scrollHeight,
      behavior: "instant",
    });
}
async function control(id, action, button) {
  button.disabled = true;
  try {
    await call("POST", API + "/" + id + "/" + action);
    if (state.selected === id) await selectRun(id);
    await loadHistory();
  } catch (error) {
    notify(explain(error), true);
  } finally {
    button.disabled = false;
  }
}
function repositoryValid(value) {
  try {
    const url = new URL(value);
    return (
      url.protocol === "https:" &&
      url.hostname === "github.com" &&
      !url.username &&
      !url.password &&
      !url.port &&
      !url.search &&
      !url.hash &&
      /^\/[^/]+\/[^/]+(?:\/(?:tree|commit)\/.+)?\/?$/.test(url.pathname)
    );
  } catch {
    return false;
  }
}
async function submit(event) {
  event.preventDefault();
  if (state.busy) return;
  const task = $("question").value.trim();
  const repository = $("repository-url").value.trim();
  if (!task) return;
  if (!state.connected) {
    showSettings();
    return;
  }
  if (!state.selected && !repositoryValid(repository)) {
    notify(
      "请输入完整的 GitHub 仓库 URL，例如 https://github.com/owner/repository。",
      true,
    );
    return;
  }
  state.busy = true;
  const parent = state.selected;
  updateComposer();
  notify(parent ? "正在提交追问…" : "正在连接仓库并固定代码版本…");
  try {
    if (!isDemo()) {
      state.profile = await call("GET", API + "/profile");
      renderProfile();
      if (!state.profile.ready) {
        showSettings();
        notify("请先完成服务端配置，问题和仓库地址已保留。");
        return;
      }
    }
    let path = API,
      body;
    if (parent) {
      path += "/" + parent + "/messages";
      body = { body: task };
    } else {
      body = {
        repository_url: repository,
        task,
        execution_mode: state.demo ? "fixture" : "live",
      };
      if (!state.demo) body.limits = state.profile.limits;
      const ref = $("repository-ref").value.trim();
      if (ref) {
        if (/^[a-fA-F0-9]{40}$/.test(ref)) body.commit = ref;
        else body.ref = ref;
      }
    }
    // Keep the same id after an ambiguous network failure; the server owns idempotency.
    const fingerprint = JSON.stringify({ path, body });
    if (state.request?.fingerprint !== fingerprint)
      state.request = { fingerprint, id: crypto.randomUUID() };
    body.client_request_id = state.request.id;
    const run = await call("POST", path, body);
    state.request = null;
    // The POST succeeded even if a later refresh fails. Keep its identity so
    // a temporary read failure cannot turn another click into duplicate work.
    state.selected = run.run_id;
    state.details.set(run.run_id, {
      run,
      tool_calls: [],
      claims: [],
      report: null,
    });
    save("mgar-selected", run.run_id);
    $("question").value = "";
    $("welcome").hidden = true;
    $("conversation").hidden = false;
    renderConversation();
    await selectRun(run.run_id);
    await loadHistory();
    notify("");
  } catch (error) {
    notify(explain(error), true);
  } finally {
    state.busy = false;
    updateComposer();
  }
}
async function poll() {
  if (!state.connected || state.polling || document.hidden) return;
  state.polling = true;
  const selected = state.selected,
    version = state.selection;
  try {
    if (selected) {
      const detail = await call("GET", API + "/" + selected);
      if (selected === state.selected && version === state.selection) {
        state.details.set(selected, detail);
        renderConversation();
        updateComposer();
      }
    }
    await loadHistory();
  } catch (error) {
    notify(explain(error), true);
  } finally {
    state.polling = false;
  }
}
$("open-settings").addEventListener("click", showSettings);
$("close-settings").addEventListener("click", () =>
  $("settings-dialog").close(),
);
$("connect-form").addEventListener("submit", connect);
$("new-chat").addEventListener("click", newChat);
$("refresh-history").addEventListener("click", () =>
  loadHistory().catch((error) => notify(explain(error), true)),
);
$("toggle-sidebar").addEventListener("click", () =>
  $("sidebar").classList.toggle("open"),
);
$("demo-mode").addEventListener("change", () => {
  state.demo = $("demo-mode").checked;
  save("mgar-demo", state.demo ? "1" : "");
  updateMode();
  if (state.selected) newChat();
});
$("composer").addEventListener("submit", submit);
$("question").addEventListener("keydown", (event) => {
  if (
    (event.ctrlKey || event.metaKey) &&
    event.key === "Enter" &&
    !$("send").disabled
  )
    $("composer").requestSubmit();
});
for (const button of document.querySelectorAll("[data-prompt]"))
  button.addEventListener("click", () => {
    $("question").value = button.dataset.prompt;
    $("question").focus();
  });
window.addEventListener("pagehide", () => clearInterval(state.timer));
window.addEventListener("pageshow", () => {
  clearInterval(state.timer);
  state.timer = setInterval(poll, 1800);
});
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) poll();
});
updateMode();
(async () => {
  if (!state.token) return;
  await connect();
  const previous = saved("mgar-selected");
  if (state.connected && previous) {
    try {
      await selectRun(previous);
    } catch {
      save("mgar-selected", "");
    }
  }
})();
