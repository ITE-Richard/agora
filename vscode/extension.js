// Agora VSCode 擴充套件：選擇參與的 AI 與模型、管理討論串、查看額度。
// 所有設定與動作都透過 agora.py 完成（設定只有一份：<工作區>/.agora/config.json），討論串直接讀 state.json 顯示。

const vscode = require("vscode");
const cp = require("child_process");
const fs = require("fs");
const path = require("path");

const PARTIES = ["claude", "antigravity", "codex"];
const NAMES = { claude: "Claude Code", antigravity: "Antigravity", codex: "Codex", human: "我" };
const SHORT = { claude: "Claude", antigravity: "Agy", codex: "Codex" };
const ACTIVE_RELAY = ["starting", "running", "waiting"];
const RELAY_LABELS = {
  starting: "啟動中", running: "執行中", waiting: "等額度重置", done: "已完成", stopped: "已暫停",
  failed: "失敗", max_rounds: "已達最大輪數",
};

let output;
let extensionPath;

// ───────────────────────── 執行 agora.py ─────────────────────────

function agoraRoot() {
  const configured = vscode.workspace.getConfiguration("agora").get("root");
  const candidates = [configured];
  try {
    candidates.push(JSON.parse(fs.readFileSync(path.join(extensionPath, "agora-root.json"), "utf8")).root);
  } catch {}
  try {
    candidates.push(path.resolve(fs.realpathSync(extensionPath), ".."));   // 直接從 repo 載入（開發模式）
  } catch {}
  return candidates.find((c) => c && fs.existsSync(path.join(c, "agora.py")));
}

function workspaceRoot() {
  const folders = vscode.workspace.workspaceFolders;
  return folders && folders.length ? folders[0].uri.fsPath : undefined;
}

class AgoraError extends Error {}

function killTree(proc) {
  if (process.platform === "win32") {
    cp.spawn("taskkill", ["/pid", String(proc.pid), "/T", "/F"]);
  } else {
    proc.kill();
  }
}

/** 執行 agora.py；失敗時丟出 AgoraError（訊息為 agora.py 印在 stderr 的最後一段） */
function runAgora(args, { input, token, onProgress, log = true } = {}) {
  const root = agoraRoot();
  const ws = workspaceRoot();
  if (!root) {
    return Promise.reject(new AgoraError("找不到 agora.py，請在設定 agora.root 指定 Agora 程式所在的資料夾。"));
  }
  if (!ws) {
    return Promise.reject(new AgoraError("請先開啟一個資料夾。"));
  }
  const python = vscode.workspace.getConfiguration("agora").get("pythonPath") || "python";
  const fullArgs = [path.join(root, "agora.py"), "--workspace", ws, ...args];
  if (log) {
    output.appendLine(`$ agora ${args.join(" ")}`);
  }
  return new Promise((resolve, reject) => {
    const proc = cp.spawn(python, fullArgs, {
      cwd: ws,
      env: { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONUTF8: "1" },
      windowsHide: true,
    });
    let stdout = "";
    let stderr = "";
    proc.stdout.setEncoding("utf8");
    proc.stderr.setEncoding("utf8");
    proc.stdout.on("data", (d) => {
      stdout += d;
      if (log) output.append(d);
    });
    proc.stderr.on("data", (d) => {
      stderr += d;
      if (log) output.append(d);
      const line = d.trim().split("\n").pop();
      if (onProgress && line) onProgress(line);
    });
    proc.on("error", (e) => reject(new AgoraError(`無法執行 ${python}：${e.message}`)));
    proc.on("close", (code) => {
      if (code === 0) {
        resolve(stdout);
      } else if (token && token.isCancellationRequested) {
        reject(new AgoraError("已取消。"));
      } else {
        const message = stderr.trim().split("\n").slice(-3).join("\n") || stdout.trim().split("\n").pop();
        reject(new AgoraError(message || `agora ${args[0]} 失敗（exit ${code}）`));
      }
    });
    if (token) token.onCancellationRequested(() => killTree(proc));
    proc.stdin.end(input || "");
  });
}

async function runJson(args) {
  return JSON.parse(await runAgora(args, { log: false }));
}

/** 需要等 AI 回覆的動作：顯示可取消的進度通知 */
const BACKGROUND_COMMANDS = ["send", "reply", "auto"];

/** 討論在 agora.py 的背景程序執行：取消只是停止等待，討論會繼續，完成時另有通知 */
async function runLong(title, args, input) {
  const background = BACKGROUND_COMMANDS.includes(args[0]);
  try {
    return await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: `Agora：${title}`, cancellable: true },
      (progress, token) => runAgora(args, { input, token, onProgress: (m) => progress.report({ message: m }) }),
    );
  } catch (e) {
    if (background && e instanceof AgoraError && e.message === "已取消。") {
      vscode.window.showInformationMessage("已停止等待，討論在背景繼續，完成時會通知。要停止討論，請在討論串上按右鍵選「停止討論」。");
      return "";
    }
    throw e;
  }
}

function showError(e) {
  if (e instanceof AgoraError) {
    vscode.window.showErrorMessage(`Agora：${e.message}`);
  } else {
    output.appendLine(String(e && e.stack || e));
    vscode.window.showErrorMessage(`Agora：${e && e.message || e}`);
  }
}

// ───────────────────────── 討論串 ─────────────────────────

function threadsDir() {
  const ws = workspaceRoot();
  return ws && path.join(ws, ".agora", "threads");
}

function loadThreads() {
  const dir = threadsDir();
  if (!dir || !fs.existsSync(dir)) return [];
  const threads = [];
  for (const name of fs.readdirSync(dir)) {
    try {
      const state = JSON.parse(fs.readFileSync(path.join(dir, name, "state.json"), "utf8"));
      threads.push({ id: name, dir: path.join(dir, name), state });
    } catch {}
  }
  return threads.sort((a, b) => b.id.localeCompare(a.id));
}

class ThreadsProvider {
  constructor(activity) {
    this.activity = activity;
    this.emitter = new vscode.EventEmitter();
    this.onDidChangeTreeData = this.emitter.event;
  }

  refresh() {
    this.emitter.fire();
  }

  getChildren(element) {
    return element ? [] : loadThreads();
  }

  getTreeItem(thread) {
    const { state } = thread;
    const item = new vscode.TreeItem(state.topic, vscode.TreeItemCollapsibleState.None);
    const messages = state.messages || [];
    const last = messages[messages.length - 1];
    const relay = state.relay;
    const job = this.activity.discussion(thread.id);
    const parts = [];
    if (job) parts.push(job.current ? `${NAMES[job.current]} 回覆中 ${minutesSince(job.since)}` : "討論啟動中");
    parts.push(`${messages.length} 則`);
    if (relay) parts.push(`${NAMES[relay.worker]} ${RELAY_LABELS[relay.status] || relay.status}`);
    if (state.keep) parts.push("保留");
    item.description = parts.join(" · ");
    const active = relay && ACTIVE_RELAY.includes(relay.status);
    item.iconPath = new vscode.ThemeIcon(job || active ? "sync~spin" : relay ? "tasklist" : state.keep ? "pinned" : "comment-discussion");
    item.contextValue = (active ? "thread-relay-active" : relay ? "thread-relay" : "thread") + (job ? "-job" : "")
      + (state.keep ? "-kept" : "");
    const tip = new vscode.MarkdownString();
    tip.appendMarkdown(`**${state.topic}**\n\n`);
    tip.appendMarkdown(`參與者：${Object.keys(state.parties).map((p) => NAMES[p] || p).join("、")}\n\n`);
    tip.appendMarkdown(`建立：${state.created}`);
    if (last) tip.appendMarkdown(`\n\n最後發言：${NAMES[last.speaker] || last.speaker}（${last.time}）`);
    item.tooltip = tip;
    item.id = thread.id;
    item.command = { command: "agora.openTranscript", title: "開啟逐字稿", arguments: [thread] };
    return item;
  }
}

function minutesSince(ts) {
  if (!ts) return "";
  const m = Math.floor((Date.now() / 1000 - ts) / 60);
  return m < 1 ? "不到 1 分鐘" : `${m} 分鐘`;
}

/** 進行中的討論與分派（agora activity）；有東西結束時發出通知 */
class Activity {
  constructor() {
    this.items = null;
    this.emitter = new vscode.EventEmitter();
    this.onDidChange = this.emitter.event;
  }

  discussion(threadId) {
    return (this.items || []).find((x) => x.type === "discussion" && x.thread === threadId);
  }

  async load() {
    let items;
    try {
      items = JSON.parse(await runAgora(["activity", "--json"], { log: false }));
    } catch {
      return;
    }
    const before = this.items;
    this.items = items;
    if (before) {
      const key = (x) => `${x.type}:${x.thread}`;
      const now = new Set(items.map(key));
      for (const gone of before.filter((x) => !now.has(key(x)))) notifyFinished(gone);
    }
    this.emitter.fire();
  }
}

function notifyFinished(item) {
  const thread = loadThreads().find((t) => t.id === item.thread);
  if (!thread) return;
  let text;
  let warn = false;
  if (item.type === "discussion") {
    let job = {};
    try {
      job = JSON.parse(fs.readFileSync(path.join(thread.dir, "job.json"), "utf8"));
    } catch {}
    if (job.status === "stopped") return;
    warn = job.status !== "done";
    text = warn ? `「${thread.state.topic}」的討論${job.status === "interrupted" ? "被中斷" : "失敗"}，詳見逐字稿。`
      : `「${thread.state.topic}」的討論已完成。`;
  } else {
    const relay = thread.state.relay || {};
    warn = relay.status !== "done";
    text = `${NAMES[item.worker]} 接手「${thread.state.topic}」的工作：${RELAY_LABELS[relay.status] || relay.status}。`;
  }
  const shown = warn ? vscode.window.showWarningMessage(`Agora：${text}`, "開啟逐字稿")
    : vscode.window.showInformationMessage(`Agora：${text}`, "開啟逐字稿");
  shown.then((choice) => {
    if (choice) openTranscript(thread);
  });
}

async function pickThread(arg) {
  if (arg && arg.id && arg.state) return arg;
  const threads = loadThreads();
  if (!threads.length) {
    vscode.window.showInformationMessage("此專案還沒有討論串。");
    return undefined;
  }
  const picked = await vscode.window.showQuickPick(
    threads.map((t) => ({ label: t.state.topic, description: t.id, thread: t })),
    { placeHolder: "選擇討論串" },
  );
  return picked && picked.thread;
}

async function openTranscript(thread) {
  const uri = vscode.Uri.file(path.join(thread.dir, "transcript.md"));
  await vscode.commands.executeCommand("markdown.showPreview", uri);
}

/** 輸入訊息：可以直接輸入一行，或使用目前編輯器的選取內容／整份文件 */
async function askMessage(prompt) {
  const editor = vscode.window.activeTextEditor;
  if (editor && !editor.document.uri.fsPath.endsWith("transcript.md")) {
    const selection = editor.document.getText(editor.selection);
    const name = path.basename(editor.document.fileName);
    const choice = await vscode.window.showQuickPick(
      [
        { label: "$(edit) 輸入訊息", value: "type" },
        selection.trim()
          ? { label: "$(selection) 使用編輯器選取的內容", description: name, value: "selection" }
          : { label: "$(file) 使用目前編輯器的整份內容", description: name, value: "document" },
      ],
      { placeHolder: prompt },
    );
    if (!choice) return undefined;
    if (choice.value === "selection") return selection;
    if (choice.value === "document") return editor.document.getText();
  }
  const text = await vscode.window.showInputBox({ prompt, ignoreFocusOut: true });
  return text && text.trim() ? text : undefined;
}

// ───────────────────────── 參與方、模型、額度 ─────────────────────────

class Model {
  constructor() {
    this.parties = null;   // agora parties --json
    this.work = {};        // 近期各 AI 的接手輪數（同上）
    this.shareDays = 7;
    this.catalog = null;   // agora models --json
    this.quota = null;     // agora quota --json
    this.errors = {};
    this.emitter = new vscode.EventEmitter();
    this.onDidChange = this.emitter.event;
  }

  installed() {
    const ws = workspaceRoot();
    return !!ws && fs.existsSync(path.join(ws, ".agora", "config.json"));
  }

  enabled() {
    return this.parties ? PARTIES.filter((p) => this.parties[p].enabled) : PARTIES;
  }

  async loadParties() {
    try {
      this.setParties(await runJson(["parties", "--json"]));
      delete this.errors.parties;
    } catch (e) {
      this.errors.parties = e.message;
    }
    this.emitter.fire();
  }

  setParties(data) {
    this.parties = data.parties;
    this.work = data.work || {};
    this.shareDays = data.share_days || 7;
  }

  async loadCatalog(refresh = false) {
    try {
      this.catalog = await runJson(["models", "--json", ...(refresh ? ["--refresh"] : [])]);
      delete this.errors.catalog;
    } catch (e) {
      this.errors.catalog = e.message;
    }
    this.emitter.fire();
  }

  async loadQuota(refresh = false) {
    try {
      this.quota = await runJson(["quota", "--json", ...(refresh ? ["--refresh"] : [])]);
      delete this.errors.quota;
    } catch (e) {
      this.errors.quota = e.message;
    }
    this.emitter.fire();
  }

  async update(args) {
    try {
      this.setParties(await runJson(["parties", ...args, "--json"]));
    } catch (e) {
      showError(e);
    }
    this.emitter.fire();
  }

  snapshot() {
    return {
      workspace: workspaceRoot(),
      agoraFound: !!agoraRoot(),
      installed: this.installed(),
      parties: this.parties,
      work: this.work,
      shareDays: this.shareDays,
      catalog: this.catalog,
      quota: this.quota,
      errors: this.errors,
      names: NAMES,
      order: PARTIES,
    };
  }
}

function quotaText(u) {
  if (!u) return "無資料";
  const reset = u.resets_at ? new Date(u.resets_at * 1000) : null;
  const sameDay = reset && reset.toDateString() === new Date().toDateString();
  const when = reset
    ? reset.toLocaleString("zh-TW", sameDay ? { hour: "2-digit", minute: "2-digit", hour12: false }
      : { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false })
    : "未知";
  return `剩 ${Math.round(u.remaining_pct)}%（${when} 重置）`;
}

class PartiesView {
  constructor(model) {
    this.model = model;
    model.onDidChange(() => this.post());
  }

  resolveWebviewView(view) {
    this.view = view;
    const media = vscode.Uri.file(path.join(extensionPath, "media"));
    view.webview.options = { enableScripts: true, localResourceRoots: [media] };
    const nonce = Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2);
    const js = view.webview.asWebviewUri(vscode.Uri.joinPath(media, "panel.js"));
    const css = view.webview.asWebviewUri(vscode.Uri.joinPath(media, "panel.css"));
    view.webview.html = `<!DOCTYPE html>
<html lang="zh-TW"><head><meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${view.webview.cspSource}; script-src 'nonce-${nonce}';">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="stylesheet" href="${css}"></head>
<body><div id="app">載入中…</div><script nonce="${nonce}" src="${js}"></script></body></html>`;
    view.webview.onDidReceiveMessage((msg) => this.onMessage(msg).catch(showError));
    view.onDidChangeVisibility(() => view.visible && this.post());
  }

  post() {
    if (this.view) this.view.webview.postMessage({ type: "state", state: this.model.snapshot() });
  }

  async onMessage(msg) {
    const m = this.model;
    switch (msg.type) {
      case "ready":
        this.post();
        if (!m.parties) m.loadParties();
        if (!m.catalog) m.loadCatalog();
        if (!m.quota) m.loadQuota();
        break;
      case "toggle":
        await m.update([msg.enabled ? "--enable" : "--disable", msg.party]);
        break;
      case "set":
        await m.update([`--${msg.field}`, `${msg.party}=${msg.value || "default"}`]);
        break;
      case "custom": {
        const current = m.parties && m.parties[msg.party].model;
        const value = await vscode.window.showInputBox({
          prompt: `${NAMES[msg.party]} 的模型名稱（CLI 接受的完整名稱或別名）`,
          value: current || "",
        });
        if (value !== undefined) await m.update(["--model", `${msg.party}=${value.trim() || "default"}`]);
        else this.post();
        break;
      }
      case "refreshModels":
        await m.loadCatalog(true);
        break;
      case "refreshQuota":
        await m.loadQuota(true);
        break;
      case "install":
        await vscode.commands.executeCommand("agora.install");
        break;
      case "newThread":
        await vscode.commands.executeCommand("agora.newThread");
        break;
      case "settings":
        await vscode.commands.executeCommand("workbench.action.openSettings", "agora.");
        break;
    }
  }
}

// ───────────────────────── 啟動 ─────────────────────────

function activate(context) {
  extensionPath = context.extensionPath;
  output = vscode.window.createOutputChannel("Agora");
  const model = new Model();
  const activity = new Activity();
  const threads = new ThreadsProvider(activity);
  activity.onDidChange(() => threads.refresh());

  const activityBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 51);
  activityBar.command = "workbench.view.extension.agora";
  activity.onDidChange(() => {
    const items = activity.items || [];
    if (!items.length) {
      activityBar.hide();
      return;
    }
    activityBar.text = `$(sync~spin) Agora ${items.length} 進行中`;
    activityBar.tooltip = "Agora 進行中（關閉視窗也會繼續）\n" + items.map((x) => x.type === "discussion"
      ? `💬 ${x.topic}：${x.current ? `${NAMES[x.current]} 回覆中（${minutesSince(x.since)}）` : "啟動中"}`
      : `🛠 ${x.topic}：${NAMES[x.worker]} 接手${RELAY_LABELS[x.status] || x.status}，已跑 ${x.rounds} 輪`).join("\n");
    activityBar.show();
  });
  context.subscriptions.push(activityBar);
  const panel = new PartiesView(model);

  const statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 50);
  statusBar.command = "workbench.view.extension.agora";
  const updateStatusBar = () => {
    if (!model.quota || !workspaceRoot()) {
      statusBar.hide();
      return;
    }
    const enabled = model.enabled();
    const threshold = model.quota.threshold_pct || 5;
    statusBar.text = "$(organization) " + enabled.map((p) => {
      const u = model.quota[p];
      const pct = u ? `${Math.round(u.remaining_pct)}%` : "?";
      return `${SHORT[p]} ${pct}`;
    }).join(" · ");
    const low = enabled.some((p) => model.quota[p] && model.quota[p].remaining_pct < threshold);
    statusBar.backgroundColor = low ? new vscode.ThemeColor("statusBarItem.warningBackground") : undefined;
    statusBar.tooltip = "Agora 額度\n" + enabled.map((p) => `${NAMES[p]}：${quotaText(model.quota[p])}`).join("\n");
    statusBar.show();
  };
  model.onDidChange(updateStatusBar);

  const refreshAll = () => {
    threads.refresh();
    model.loadParties();
  };

  const command = (id, fn) => context.subscriptions.push(
    vscode.commands.registerCommand(id, (...a) => Promise.resolve(fn(...a)).catch(showError)));

  command("agora.refresh", async () => {
    refreshAll();
    model.loadQuota();
    await activity.load();
  });

  command("agora.install", async () => {
    const out = await runAgora(["install"]);
    vscode.window.showInformationMessage(out.trim().split("\n")[0]);
    refreshAll();
  });

  command("agora.openTranscript", async (arg) => {
    const thread = await pickThread(arg);
    if (thread) await openTranscript(thread);
  });

  command("agora.newThread", async () => {
    if (!model.installed()) {
      const go = await vscode.window.showWarningMessage("此專案尚未安裝 Agora，要先安裝嗎？", "安裝", "直接建立");
      if (!go) return;
      if (go === "安裝") await vscode.commands.executeCommand("agora.install");
    }
    const topic = await vscode.window.showInputBox({ prompt: "討論主題", ignoreFocusOut: true });
    if (!topic || !topic.trim()) return;
    await model.loadParties();
    let parties = model.enabled();
    if (parties.length > 1) {
      const picked = await vscode.window.showQuickPick(
        parties.map((p) => ({ label: NAMES[p], party: p, picked: true })),
        { canPickMany: true, placeHolder: "參與這個討論串的 AI（預設為此專案啟用的全部 AI）" },
      );
      if (!picked || !picked.length) return;
      parties = picked.map((x) => x.party);
    }
    const id = (await runAgora(["new", topic.trim(), "--parties", parties.join(",")])).trim().split("\n").pop();
    threads.refresh();
    const thread = loadThreads().find((t) => t.id === id);
    if (!thread) return;
    await openTranscript(thread);
    const opening = await vscode.window.showInputBox({
      prompt: "開場訊息（會請所有 AI 依序回覆；留空表示稍後再發言）", ignoreFocusOut: true,
    });
    if (opening && opening.trim()) {
      await runLong("等待 AI 回覆", ["send", id, "--from", "human", "--to", "all"], opening);
    }
  });

  command("agora.send", async (arg) => {
    const thread = await pickThread(arg);
    if (!thread) return;
    const text = await askMessage(`在「${thread.state.topic}」發言`);
    if (!text) return;
    const enabled = model.enabled();
    const inThread = Object.keys(thread.state.parties).filter((p) => enabled.includes(p));
    const target = await vscode.window.showQuickPick(
      [
        { label: "$(organization) 所有 AI 依序回覆", description: inThread.map((p) => NAMES[p]).join("、"), to: "all" },
        ...enabled.map((p) => ({ label: `$(person) 只請 ${NAMES[p]} 回覆`, to: p })),
        { label: "$(note) 只記錄，不請任何人回覆", to: "none" },
      ],
      { placeHolder: "請誰回覆？" },
    );
    if (!target) return;
    await openTranscript(thread);
    await runLong(target.to === "none" ? "記錄訊息" : "等待 AI 回覆",
      ["send", thread.id, "--from", "human", "--to", target.to], text);
  });

  command("agora.auto", async (arg) => {
    const thread = await pickThread(arg);
    if (!thread) return;
    const rounds = await vscode.window.showInputBox({
      prompt: "讓 AI 依序自動發言幾輪？（出現【已達成共識】會提前結束）", value: "4",
      validateInput: (v) => (/^\d+$/.test(v) && +v > 0 ? undefined : "請輸入正整數"),
    });
    if (!rounds) return;
    await openTranscript(thread);
    await runLong("AI 自動討論中", ["auto", thread.id, "--rounds", rounds]);
  });

  command("agora.assign", async (arg) => {
    const thread = await pickThread(arg);
    if (!thread) return;
    const enabled = model.enabled();
    const worker = await vscode.window.showQuickPick(
      [
        { label: "$(sparkle) 依分配比例自動挑選", description: "略過額度不足與比例為 0 的 AI", to: null },
        ...enabled.map((p) => ({
          label: NAMES[p], description: model.quota ? quotaText(model.quota[p]) : "", to: p,
        })),
      ],
      { placeHolder: "把工作交給誰？接手方會在背景執行，只能修改工作區內的檔案、不能 commit" },
    );
    if (!worker) return;
    const text = await askMessage("工作單：範圍、不能碰的檔案、驗收方式");
    if (!text) return;
    const out = await runAgora(["assign", thread.id, "--from", "human", ...(worker.to ? ["--to", worker.to] : [])],
      { input: text });
    const lines = out.trim().split("\n").filter((l) => !l.startsWith("進度：") && !l.startsWith("查詢："));
    vscode.window.showInformationMessage(lines.join(" "));
    threads.refresh();
  });

  command("agora.status", async (arg) => {
    const thread = await pickThread(arg);
    if (!thread) return;
    const out = await runAgora(["status", thread.id]);
    output.show(true);
    const choice = await vscode.window.showInformationMessage(out.trim().split("\n")[0], "開啟進度檔");
    if (choice) {
      await vscode.window.showTextDocument(vscode.Uri.file(path.join(thread.dir, "progress.md")));
    }
  });

  command("agora.recall", async (arg) => {
    const thread = await pickThread(arg);
    if (!thread) return;
    const ok = await vscode.window.showWarningMessage(
      `要請 ${NAMES[thread.state.relay && thread.state.relay.worker] || "接手方"} 在下一個檢查點暫停嗎？`,
      { modal: true }, "收回");
    if (!ok) return;
    vscode.window.showInformationMessage((await runAgora(["recall", thread.id])).trim());
    threads.refresh();
  });

  const fmtSize = (bytes) => (bytes >= 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)} MB` : `${Math.ceil(bytes / 1024)} KB`);
  const sessionNames = (info) => [...new Set(info.sessions.map((s) => NAMES[s.party]))].join("、") || "無";

  command("agora.prune", async () => {
    const days = await vscode.window.showInputBox({
      prompt: "清理超過幾天沒有活動的討論串？（標記保留、分派工作未完成、使用中的一律不清）", value: "30",
      validateInput: (v) => (/^\d+(\.\d+)?$/.test(v) ? undefined : "請輸入天數"),
    });
    if (!days) return;
    const scan = JSON.parse(await runAgora(["prune", "--days", days, "--json"], { log: false }));
    if (!scan.candidates.length) {
      vscode.window.showInformationMessage(`沒有可清理的討論串（另有 ${scan.kept.length} 個保留）。`);
      return;
    }
    const picked = await vscode.window.showQuickPick(
      scan.candidates.map((c) => ({
        label: c.topic, picked: true, info: c,
        description: `閒置 ${c.idle_days} 天 · ${c.messages} 則 · ${fmtSize(c.bytes)}`,
        detail: `${c.id} · AI 對話紀錄：${sessionNames(c)}`,
      })),
      { canPickMany: true, placeHolder: `勾選要刪除的討論串（另有 ${scan.kept.length} 個不符合條件、會保留）` },
    );
    if (!picked || !picked.length) return;
    const ok = await vscode.window.showWarningMessage(
      `刪除 ${picked.length} 個討論串，以及 Agora 為它們在各 AI CLI 開的對話紀錄？此動作無法復原。`,
      { modal: true }, "刪除", "只刪討論串，保留對話紀錄");
    if (!ok) return;
    const args = ["prune", "--yes", ...picked.flatMap((x) => ["--thread", x.info.id])];
    if (ok !== "刪除") args.push("--keep-sessions");
    const result = JSON.parse(await runAgora([...args, "--json"]));
    threads.refresh();
    const msg = `已刪除 ${result.deleted.length} 個討論串。`;
    if (result.errors.length) {
      output.appendLine(result.errors.join("\n"));
      vscode.window.showWarningMessage(`${msg}有 ${result.errors.length} 筆對話紀錄沒有刪除，詳見 Agora 輸出。`);
    } else {
      vscode.window.showInformationMessage(msg);
    }
  });

  command("agora.deleteThread", async (arg) => {
    const thread = await pickThread(arg);
    if (!thread) return;
    const scan = JSON.parse(await runAgora(["prune", "--thread", thread.id, "--json"], { log: false }));
    if (!scan.candidates.length) {
      vscode.window.showWarningMessage(`「${thread.state.topic}」不能刪除：${scan.kept[0].reason}。`);
      return;
    }
    const ok = await vscode.window.showWarningMessage(
      `刪除「${thread.state.topic}」？會一併刪除 Agora 為它在各 AI CLI 開的對話紀錄（${sessionNames(scan.candidates[0])}），無法復原。`,
      { modal: true }, "刪除", "只刪討論串，保留對話紀錄");
    if (!ok) return;
    await runAgora(["prune", "--yes", "--thread", thread.id, ...(ok === "刪除" ? [] : ["--keep-sessions"])]);
    threads.refresh();
  });

  command("agora.keep", async (arg) => {
    const thread = await pickThread(arg);
    if (thread) await runAgora(["keep", thread.id]);
    threads.refresh();
  });

  command("agora.unkeep", async (arg) => {
    const thread = await pickThread(arg);
    if (thread) await runAgora(["keep", thread.id, "--off"]);
    threads.refresh();
  });

  command("agora.stopJob", async (arg) => {
    const thread = await pickThread(arg);
    if (!thread) return;
    const ok = await vscode.window.showWarningMessage(
      `停止「${thread.state.topic}」進行中的討論？已收到的回覆都已寫入逐字稿。`, { modal: true }, "停止");
    if (!ok) return;
    vscode.window.showInformationMessage((await runAgora(["stop", thread.id])).trim());
    activity.load();
  });

  command("agora.quota", async () => {
    await runLong("查詢額度", ["quota", "--refresh"]);
    output.show(true);
    model.loadQuota();
  });

  command("agora.check", async () => {
    try {
      await runLong("執行驗證", ["check"]);
      vscode.window.showInformationMessage("Agora：驗證通過。");
    } finally {
      output.show(true);
    }
  });

  context.subscriptions.push(
    output,
    statusBar,
    vscode.window.registerWebviewViewProvider("agora.parties", panel),
    vscode.window.registerTreeDataProvider("agora.threads", threads),
  );

  // 討論串與設定檔變動時自動更新（AI 回覆、背景接手、從終端機改設定）
  const ws = workspaceRoot();
  if (ws) {
    const watcher = vscode.workspace.createFileSystemWatcher(new vscode.RelativePattern(ws, ".agora/**/*.json"));
    let timer;
    const onChange = (uri) => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        threads.refresh();
        model.loadParties();   // 設定變更，或分派進度改變了近期工作量
        activity.load();
      }, 300);
    };
    watcher.onDidChange(onChange);
    watcher.onDidCreate(onChange);
    watcher.onDidDelete(onChange);
    context.subscriptions.push(watcher);
  }

  context.subscriptions.push(vscode.workspace.onDidChangeConfiguration((e) => {
    if (e.affectsConfiguration("agora")) refreshAll();
  }));

  const minutes = vscode.workspace.getConfiguration("agora").get("quotaRefreshMinutes");
  if (ws && fs.existsSync(path.join(ws, ".agora"))) {
    model.loadParties();
    model.loadQuota();
    activity.load();
  }
  const activityTimer = setInterval(() => {
    if (activity.items && activity.items.length) activity.load();   // 更新經過時間、偵測被強制結束的程序
  }, 30 * 1000);
  context.subscriptions.push({ dispose: () => clearInterval(activityTimer) });
  if (minutes > 0) {
    const interval = setInterval(() => model.loadQuota(), minutes * 60 * 1000);
    context.subscriptions.push({ dispose: () => clearInterval(interval) });
  }
}

function deactivate() {}

module.exports = { activate, deactivate };
