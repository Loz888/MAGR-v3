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
  AUTH_REQUIRED: "请先登录后继续。",
  LOGIN_FAILED: "用户名或密码不正确。",
  CSRF_REQUIRED: "登录状态已更新，请刷新页面后重试。",
  SETTINGS_STALE: "配置已被更新，请重新打开设置再保存。",
  ANALYSIS_BUSY: "有分析正在执行，请先暂停或等待完成后再保存设置。",
  REPO_ANALYSIS_AUTHORIZATION_CHANGED:
    "使用许可已更新，请新建分析以使用新配置。",
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
  TOKEN_BUDGET_EXHAUSTED: "本次 Token 预算已用尽，已保留当前证据和未完成项。",
  AGENT_STALLED:
    "现有线索暂时无法继续推进，已保留调查结果。可以补充文件位置、错误信息或预期行为后追问。",
  EVIDENCE_REJECTED: "当前回答尚未通过核验，以下保留已确认的证据和待解决问题。",
  INSUFFICIENT_EVIDENCE:
    "现有源码证据不足以回答问题，可以补充更具体的目标后继续。",
  MODEL_RATE_LIMITED: "模型服务暂时限流，本次进展已保留。",
  BUDGET_EXHAUSTED: "本次费用预算已用尽，以下保留已获得的结果。",
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
  csrf: "",
  username: "",
  setupRequired: false,
  revision: 0,
  pendingSubmit: false,
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
  stream: null,
  streamVersion: 0,
  cursor: 0,
  drafts: new Map(),
  progressTimer: null,
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
    credentials: "same-origin",
    headers: {
      "X-CSRF-Token": state.csrf,
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
    if (response.status === 401 && !path.startsWith("/api/v3/auth/")) {
      showLogin();
    }
    throw error;
  }
  return data;
}
function showLogin() {
  closeStream();
  state.connected = false;
  state.csrf = "";
  $("app-shell").hidden = true;
  $("auth-screen").hidden = false;
  if ($("settings-dialog").open) $("settings-dialog").close();
}
function configureLogin(setup) {
  state.setupRequired = setup;
  $("auth-eyebrow").textContent = setup ? "第一次见面，你好" : "欢迎回来";
  $("auth-title").textContent = setup ? "创建你的管理员账户" : "登录 RepoSage";
  $("auth-description").textContent = setup
    ? "为这台电脑上的 RepoSage 设置用户名与密码。"
    : "使用你的账户，继续分析项目。";
  $("login-submit").textContent = setup ? "创建账户并开始" : "登录";
  $("password").autocomplete = setup ? "new-password" : "current-password";
  $("password").minLength = setup ? 10 : 1;
  $("password").placeholder = setup ? "设置至少 10 位密码" : "输入密码";
  $("confirm-password-row").hidden = !setup;
  $("confirm-password").required = setup;
  $("auth-footnote").textContent = setup
    ? "首次创建后，仅此管理员账户可登录和修改模型设置。"
    : "登录后可直接提交问题，配置会保留在本机服务中。";
}
async function showSettings() {
  if (!state.connected) return;
  if (!$("settings-dialog").open) $("settings-dialog").showModal();
  $("settings-error").textContent = "";
  try {
    await loadSettings();
  } catch (error) {
    $("settings-error").textContent = explain(error);
  }
}
function renderProfile() {
  const profile = state.profile;
  $("readiness").hidden = !profile;
  $("checklist").replaceChildren();
  $("setup-help").replaceChildren();
  if (!profile) return;
  const checks = profile.checks || {};
  const rows = [
    [checks.model_configured, "模型密钥"],
    [checks.github_configured, "仓库读取凭据"],
    [checks.paid_enabled && checks.authorization_valid, "分析使用许可"],
  ];
  for (const [ok, label] of rows) {
    const item = element("li");
    item.append(
      element("span", ok ? "pass" : "missing", ok ? "✓" : "○"),
      element("span", "", label + (ok ? "已就绪" : "待填写")),
    );
    $("checklist").append(item);
  }
  $("profile-summary").textContent = profile.model
    ? "当前模型：" +
      profile.model.model_id +
      " · 每次最多 $" +
      (profile.limits.max_cost_micros / 1000000).toFixed(2)
    : "填写上方设置并保存，即可开始真实分析。";
  $("model-label").textContent = profile.model
    ? profile.model.model_id
    : "REPOSITORY AGENT";
  $("connection-label").textContent = state.username + " · 模型设置";
  $("account-name").textContent = "已登录：" + state.username;
  $("connection-dot").className =
    "status-dot " + (profile.ready ? "ready" : "error");
}
async function loadSettings() {
  const data = await call("GET", "/api/v3/settings/analysis");
  state.revision = data.revision;
  state.profile = data.profile;
  renderProfile();
  const profile = state.profile,
    checks = profile.checks || {},
    limits = profile.limits || {};
  $("model-id").value = profile.model?.model_id || "deepseek-chat";
  $("model-key").value = "";
  $("github-key").value = "";
  $("model-key").placeholder = checks.model_configured
    ? "已配置，留空保留"
    : "粘贴 DeepSeek API Key";
  $("github-key").placeholder = checks.github_configured
    ? "已配置，留空保留"
    : "粘贴 GitHub 只读 Token";
  $("model-key-help").textContent = checks.model_configured
    ? "密钥已加密保存，填写新值可替换。"
    : "填写后加密保存在运行此服务的电脑上。";
  $("github-key-help").textContent = checks.github_configured
    ? "读取凭据已配置，留空可保留。"
    : "用于读取仓库源码，只需要读取权限。";
  const repos = profile.allowed_repositories || ["*"];
  $("all-repositories").checked = repos.includes("*");
  $("repository-scope").value = repos.filter((x) => x !== "*").join("\n");
  $("repository-scope-row").hidden = $("all-repositories").checked;
  $("cost-limit").value = (limits.max_cost_micros || 500000) / 1000000;
  $("token-limit").value =
    limits.max_total_tokens || limits.max_input_tokens || 500000;
  $("allow-paid").checked = Boolean(
    checks.paid_enabled && checks.authorization_valid,
  );
  $("save-model-settings").textContent = state.pendingSubmit
    ? "保存并开始分析"
    : "保存设置";
}
async function enterApp(session) {
  configureLogin(false);
  state.username = session.username;
  state.csrf = session.csrf_token;
  state.connected = true;
  $("auth-screen").hidden = true;
  $("app-shell").hidden = false;
  $("login-error").textContent = "";
  $("password").value = "";
  $("confirm-password").value = "";
  await loadSettings();
  await loadHistory();
  const previous = saved("mgar-selected");
  if (previous) {
    try {
      await selectRun(previous);
    } catch {
      save("mgar-selected", "");
    }
  }
  if (
    (!state.profile.ready || !state.profile.automatic_execution) &&
    !state.demo
  ) {
    await showSettings();
    if (state.profile.ready)
      notify(
        "运行策略已升级为自动持续分析，保存一次设置即可启用。原有密钥可留空保留。",
      );
  }
}
async function login(event) {
  event.preventDefault();
  if (
    state.setupRequired &&
    $("password").value !== $("confirm-password").value
  ) {
    $("login-error").textContent = "两次输入的密码不一致。";
    return;
  }
  $("login-submit").disabled = true;
  $("login-error").textContent = "";
  try {
    const session = await call(
      "POST",
      "/api/v3/auth/" + (state.setupRequired ? "setup" : "login"),
      { username: $("username").value.trim(), password: $("password").value },
    );
    await enterApp(session);
  } catch (error) {
    if (state.connected) notify(explain(error), true);
    else $("login-error").textContent = explain(error);
  } finally {
    $("login-submit").disabled = false;
  }
}
async function saveModelSettings(event) {
  event.preventDefault();
  $("settings-error").textContent = "";
  const allowed = $("all-repositories").checked
    ? ["*"]
    : $("repository-scope")
        .value.split(/[\n,]+/)
        .map((x) => x.trim())
        .filter(Boolean);
  if (!allowed.length) {
    $("settings-error").textContent = "请至少填写一个允许分析的仓库。";
    return;
  }
  if (state.pendingSubmit && !$("allow-paid").checked) {
    $("settings-error").textContent =
      "开始真实分析前，请确认使用许可与费用上限。";
    return;
  }
  const body = {
    expected_revision: state.revision,
    model_id: $("model-id").value.trim(),
    allowed_repositories: allowed,
    allow_paid: $("allow-paid").checked,
    authorization_days: Number($("authorization-days").value),
    limits: {
      max_model_calls: null,
      max_tool_calls: null,
      max_input_tokens: Number($("token-limit").value),
      max_total_tokens: Number($("token-limit").value),
      max_cost_micros: Math.round(Number($("cost-limit").value) * 1000000),
      max_seconds: 3600,
    },
  };
  if ($("model-key").value.trim())
    body.deepseek_api_key = $("model-key").value.trim();
  if ($("github-key").value.trim())
    body.github_read_token = $("github-key").value.trim();
  $("save-model-settings").disabled = true;
  try {
    const data = await call("PUT", "/api/v3/settings/analysis", body);
    state.profile = data.profile;
    state.revision = data.revision;
    $("model-key").value = "";
    $("github-key").value = "";
    renderProfile();
    if (state.profile.ready) {
      $("settings-dialog").close();
      notify("设置已保存，可以开始分析。");
      if (state.pendingSubmit) {
        state.pendingSubmit = false;
        $("composer").requestSubmit();
      }
    } else {
      $("settings-error").textContent =
        "设置已保存，请补齐上方凭据并确认使用许可。";
    }
  } catch (error) {
    $("settings-error").textContent = explain(error);
  } finally {
    $("save-model-settings").disabled = false;
  }
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
  closeStream();
  state.selection++;
  state.selected = null;
  state.signature = "";
  state.request = null;
  state.pendingSubmit = false;
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
  state.drafts.set(id, detail.draft || {});
  state.cursor = detail.last_event_id || 0;
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
  subscribeRun(id);
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
        " · 已使用 " +
        (
          run.input_tokens_used + (run.output_tokens_used || 0)
        ).toLocaleString() +
        " Token",
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
  if (
    reason &&
    reason !== "complete" &&
    !["queued", "running", "paused"].includes(run.run_status)
  ) {
    result.append(
      element(
        "div",
        "run-error",
        (ERRORS[reason] || "本次分析已停止，请保留运行记录并查看服务器日志。") +
          "",
      ),
    );
  }
  const draft = element("div", "stream-draft");
  const currentDraft = state.drafts.get(run.run_id) || detail.draft || {};
  renderDraftContent(draft, currentDraft, ACTIVE.has(run.run_status));
  draft.hidden = Boolean(detail.report) || !Object.keys(currentDraft).length;
  result.append(draft);
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
    showLogin();
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
      if (!state.profile.ready || !state.profile.automatic_execution) {
        state.pendingSubmit = true;
        showSettings();
        notify("补齐模型设置后即可继续，问题和仓库地址已保留。");
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
function closeStream() {
  state.streamVersion++;
  if (state.stream) state.stream.close();
  state.stream = null;
  clearTimeout(state.progressTimer);
}
function renderDraftContent(host, draft, active = true) {
  host.replaceChildren(
    element(
      "p",
      "draft-note",
      active
        ? "正在生成 · 完成后核对源码与回答完整性"
        : "未完成草稿 · 尚未通过核验",
    ),
  );
  for (const [field, title] of [
    ["conclusion", "当前结论"],
    ["structure", "项目结构"],
  ]) {
    if (draft[field])
      host.append(element("h3", "", title), element("p", "", draft[field]));
  }
}
function subscribeRun(id) {
  closeStream();
  const detail = state.details.get(id);
  if (!state.connected || !detail || !ACTIVE.has(detail.run.run_status)) return;
  const version = state.streamVersion;
  const source = new EventSource(
    API + "/" + id + "/events?after=" + state.cursor,
  );
  state.stream = source;
  source.addEventListener("update", (event) => {
    if (version !== state.streamVersion || id !== state.selected) return;
    const message = JSON.parse(event.data);
    if (message.event_id <= state.cursor) return;
    state.cursor = message.event_id;
    const payload = message.payload || {};
    const draft = state.drafts.get(id) || {};
    if (
      (message.kind === "model_stream_started" &&
        payload.phase === "analysis") ||
      message.kind === "draft_rejected"
    ) {
      state.drafts.set(id, {});
      const host = document.querySelector(
        '.turn[data-run-id="' + id + '"] .stream-draft',
      );
      if (host) host.hidden = true;
    }
    if (message.kind === "answer_delta") {
      draft[payload.field] =
        Array.from(draft[payload.field] || "")
          .slice(0, payload.offset)
          .join("") + payload.text;
      state.drafts.set(id, draft);
      const host = document.querySelector(
        '.turn[data-run-id="' + id + '"] .stream-draft',
      );
      if (host) {
        host.hidden = false;
        renderDraftContent(host, draft);
      }
    }
    const labels = {
      model_stream_started:
        payload.phase === "validation" ? "正在核对回答完整性" : "正在分析",
      tool_reserved: TOOLS[payload.tool] || "正在读取代码",
      tool_observed: "正在整理源码证据",
      acceptance_started: "正在核对回答完整性",
      analysis_waiting: "正在等待模型服务",
      acceptance_passed: "核验通过",
      draft_rejected: "正在补充调查",
    };
    const label = document.querySelector(
      '.turn[data-run-id="' + id + '"] .run-status',
    );
    if (label && labels[message.kind]) label.textContent = labels[message.kind];
    if (
      ["tool_observed", "tool_failed", "model_settled"].includes(message.kind)
    ) {
      clearTimeout(state.progressTimer);
      state.progressTimer = setTimeout(async () => {
        try {
          const data = await call("GET", API + "/" + id);
          if (version !== state.streamVersion || id !== state.selected) return;
          if ((data.last_event_id || 0) >= state.cursor) {
            state.details.set(id, data);
            state.drafts.set(id, data.draft || {});
            state.cursor = data.last_event_id || state.cursor;
            renderConversation();
          }
        } catch (error) {
          notify(explain(error), true);
        }
      }, 200);
    }
  });
  source.addEventListener("done", async () => {
    if (version !== state.streamVersion || id !== state.selected) return;
    closeStream();
    try {
      await selectRun(id);
      await loadHistory();
    } catch (error) {
      notify(explain(error), true);
    }
  });
  source.addEventListener("auth_required", () => {
    closeStream();
    showLogin();
  });
  source.onopen = () => {
    if ($("notice").textContent === "连接恢复中，分析仍在后台继续。")
      notify("");
  };
  source.onerror = () => {
    if (version === state.streamVersion && id === state.selected)
      notify("连接恢复中，分析仍在后台继续。");
  };
}
async function poll() {
  if (!state.connected || state.polling || document.hidden) return;
  state.polling = true;
  const selected = state.selected,
    version = state.selection;
  try {
    if (
      selected &&
      (!state.stream || state.stream.readyState !== EventSource.OPEN)
    ) {
      const detail = await call("GET", API + "/" + selected);
      if (selected === state.selected && version === state.selection) {
        if ((detail.last_event_id || 0) >= state.cursor) {
          state.details.set(selected, detail);
          state.drafts.set(selected, detail.draft || {});
          state.cursor = detail.last_event_id || 0;
        }
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
$("close-settings").addEventListener("click", () => {
  state.pendingSubmit = false;
  $("settings-dialog").close();
});
$("settings-dialog").addEventListener("cancel", () => {
  state.pendingSubmit = false;
});
$("login-form").addEventListener("submit", login);
$("model-settings-form").addEventListener("submit", saveModelSettings);
$("all-repositories").addEventListener("change", () => {
  $("repository-scope-row").hidden = $("all-repositories").checked;
});
$("logout").addEventListener("click", async () => {
  try {
    await call("POST", "/api/v3/auth/logout");
    state.details.clear();
    state.runs = [];
    state.selected = null;
    state.pendingSubmit = false;
    save("mgar-selected", "");
    $("conversation").replaceChildren();
    newChat();
    showLogin();
    configureLogin(false);
  } catch (error) {
    notify(explain(error), true);
  }
});
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
window.addEventListener("pagehide", () => {
  clearInterval(state.timer);
  closeStream();
});
window.addEventListener("pageshow", () => {
  clearInterval(state.timer);
  state.timer = setInterval(poll, 10000);
  if (state.selected && state.connected && !state.stream)
    subscribeRun(state.selected);
});
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) poll();
});
updateMode();
save("mgar-token", ""); // Migrate away from browser-stored operator tokens.
(async () => {
  try {
    const session = await call("GET", "/api/v3/auth/session");
    configureLogin(session.setup_required);
    if (session.authenticated) await enterApp(session);
  } catch (error) {
    $("login-error").textContent = explain(error);
  }
})();
