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
function runLong(title, args, input) {
  return vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: `Agora：${title}`, cancellable: true },
    (progress, token) => runAgora(args, { input, token, onProgress: (m) => progress.report({ message: m }) }),
  );
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
  constructor() {
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
    const parts = [`${messages.length} 則`];
    if (relay) parts.push(`${NAMES[relay.worker]} ${RELAY_LABELS[relay.status] || relay.status}`);
    item.description = parts.join(" · ");
    const active = relay && ACTIVE_RELAY.includes(relay.status);
    item.iconPath = new vscode.ThemeIcon(active ? "sync~spin" : relay ? "tasklist" : "comment-discussion");
    item.contextValue = active ? "thread-relay-active" : relay ? "thread-relay" : "thread";
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
      this.parties = (await runJson(["parties", "--json"])).parties;
      delete this.errors.parties;
    } catch (e) {
      this.errors.parties = e.message;
    }
    this.emitter.fire();
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
      this.parties = (await runJson(["parties", ...args, "--json"])).parties;
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
  const threads = new ThreadsProvider();
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

  command("agora.refresh", () => {
    refreshAll();
    model.loadQuota();
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
        { label: "$(sparkle) 交給額度最多的 AI", to: null },
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
    vscode.window.showInformationMessage(out.trim().split("\n")[0]);
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
        if (uri.fsPath.endsWith("config.json")) model.loadParties();
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
  }
  if (minutes > 0) {
    const interval = setInterval(() => model.loadQuota(), minutes * 60 * 1000);
    context.subscriptions.push({ dispose: () => clearInterval(interval) });
  }
}

function deactivate() {}

module.exports = { activate, deactivate };
