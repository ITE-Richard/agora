// 「AI 與模型」面板：勾選參與的 AI、選模型與推理強度、顯示額度。狀態由擴充套件推送，變更送回擴充套件執行 agora parties。
(function () {
  const vscode = acquireVsCodeApi();
  const app = document.getElementById("app");
  let state = null;

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "class") node.className = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else if (v === true) node.setAttribute(k, "");
      else if (v !== false && v != null) node.setAttribute(k, v);
    }
    for (const c of children.flat()) {
      if (c != null && c !== false) node.append(c.nodeType ? c : String(c));
    }
    return node;
  }

  function fmtReset(ts) {
    if (!ts) return "未知";
    const d = new Date(ts * 1000);
    const pad = (n) => String(n).padStart(2, "0");
    const time = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
    return d.toDateString() === new Date().toDateString() ? time : `${d.getMonth() + 1}/${d.getDate()} ${time}`;
  }

  function singleQuotaRow(pctVal, resetTs, label, threshold) {
    const pct = Math.max(0, Math.min(100, pctVal));
    const level = pct < threshold ? "low" : pct < 25 ? "mid" : "ok";
    const fill = el("div", { class: `fill ${level}` });
    fill.style.width = `${pct}%`;
    const resetStr = fmtReset(resetTs);
    const labelPrefix = label ? `${label}：` : "";
    const title = `${labelPrefix}剩餘 ${pct.toFixed(1)}%，${resetStr} 重置`;
    return el("div", { class: "quota", title },
      label ? el("span", { class: "quota-label" }, label) : null,
      el("div", { class: "bar" }, fill),
      el("span", { class: "quota-text" }, `${Math.round(pct)}% · ${resetStr} 重置`));
  }

  function quotaBar(u, threshold) {
    if (!u) return el("div", { class: "quota muted" }, state.quota ? "額度：無資料" : "額度：查詢中…");
    const winKeys = u.windows ? Object.keys(u.windows) : [];
    if (winKeys.length > 1) {
      const rows = winKeys.map((k) => {
        const w = u.windows[k];
        return singleQuotaRow(w.remaining_pct, w.resets_at, w.label || k, threshold);
      });
      return el("div", { class: "quota-group" }, ...rows);
    }
    const label = (winKeys.length === 1 && u.windows[winKeys[0]].label) || (u.window_label && u.window ? u.window_label : null);
    const row = singleQuotaRow(u.remaining_pct, u.resets_at, label, threshold);
    if (u.pools) {
      const others = Object.entries(u.pools)
        .map(([k, v]) => `${v.label || k} ${Math.round(v.remaining_pct)}%`)
        .join("、");
      if (others) row.title += `（額度池：${others}）`;
    }
    return el("div", { class: "quota-group" }, row);
  }

  function select(options, value, onChange, disabled) {
    const s = el("select", { disabled, onchange: (e) => onChange(e.target.value) },
      options.map((o) => el("option", { value: o.value, selected: o.value === value }, o.label)));
    return s;
  }

  function partyCard(p) {
    const names = state.names;
    const setting = state.parties[p];
    const cat = (state.catalog && state.catalog[p]) || { models: [], efforts: [], default: {} };
    const def = cat.default || {};
    const disabled = !setting.enabled;

    // 模型：CLI 預設 + 目錄 + 目前的自訂值 + 自訂…
    const models = cat.models || [];
    const modelOptions = [{ value: "", label: `CLI 預設${def.model ? `（${def.model}）` : ""}` }];
    for (const m of models) modelOptions.push({ value: m.id, label: m.label === m.id ? m.id : `${m.label} · ${m.id}` });
    if (setting.model && !models.some((m) => m.id === setting.model)) {
      modelOptions.push({ value: setting.model, label: `${setting.model}（自訂）` });
    }
    modelOptions.push({ value: "__custom__", label: "自訂…" });
    const modelSelect = select(modelOptions, setting.model || "", (v) => {
      if (v === "__custom__") vscode.postMessage({ type: "custom", party: p });
      else vscode.postMessage({ type: "set", party: p, field: "model", value: v });
    }, disabled);

    // 推理強度：Codex 依所選模型支援的等級；Antigravity 的強度已包含在模型名稱裡
    const effectiveModel = models.find((m) => m.id === (setting.model || def.model));
    const efforts = (effectiveModel && effectiveModel.efforts && effectiveModel.efforts.length)
      ? effectiveModel.efforts : (cat.efforts || []);
    let effortRow = null;
    if (efforts.length) {
      const effortOptions = [{ value: "", label: `CLI 預設${def.effort ? `（${def.effort}）` : ""}` }]
        .concat(efforts.map((e) => ({ value: e, label: e })));
      if (setting.effort && !efforts.includes(setting.effort)) {
        effortOptions.push({ value: setting.effort, label: `${setting.effort}（此模型不支援）` });
      }
      effortRow = el("label", { class: "row" }, el("span", { class: "label" }, "推理強度"),
        select(effortOptions, setting.effort || "", (v) => vscode.postMessage({ type: "set", party: p, field: "effort", value: v }), disabled));
    }

    // 工作分配比例：只影響自動挑選接手方；0 表示只參與討論
    let shareRow = null;
    if (!disabled) {
      const enabled = state.order.filter((x) => state.parties[x].enabled);
      const total = enabled.reduce((sum, x) => sum + state.parties[x].share, 0);
      const done = enabled.reduce((sum, x) => sum + (state.work[x] || 0), 0);
      const mine = state.work[p] || 0;
      const target = setting.share === 0 ? "不自動分派" : total ? `目標 ${Math.round(setting.share / total * 100)}%` : "";
      const actual = `近 ${state.shareDays} 天 ${mine} 輪${done ? `（${Math.round(mine / done * 100)}%）` : ""}`;
      const input = el("input", {
        type: "number", min: "0", step: "1", value: String(setting.share), class: "share",
        title: "自動挑選接手方時的工作分配比例；0 表示只參與討論、不自動分派給它",
        onchange: (e) => {
          const v = e.target.value.trim();
          if (/^\d+$/.test(v)) vscode.postMessage({ type: "set", party: p, field: "share", value: v });
          else e.target.value = String(setting.share);
        },
      });
      shareRow = el("label", { class: "row" }, el("span", { class: "label" }, "分配比例"),
        el("span", { class: "share-line" }, input, el("span", { class: "share-text" }, `${target} · ${actual}`)));
    }

    // 內建子代理：被 Agora 呼叫時能否啟動自己的子代理；各家關閉的方式與效果不同
    let subagentRow = null;
    if (!disabled) {
      const hint = SUBAGENT_HINTS[p] || "";
      subagentRow = el("label", { class: "row", title: hint },
        el("span", { class: "label" }, "內建子代理"),
        el("span", { class: "share-line" },
          el("input", {
            type: "checkbox", checked: setting.subagents !== false,
            onchange: (e) => vscode.postMessage({ type: "set", party: p, field: "subagents", value: e.target.checked ? "on" : "off" }),
          }),
          el("span", { class: "share-text" }, setting.subagents !== false ? "允許" : `不允許 · ${SUBAGENT_OFF[p] || ""}`)));
    }

    // Codex 接手工作時的沙箱（討論一律唯讀）：跑 west build 等需寫入工作區外快取時可設為完整存取
    let sandboxRow = null;
    if (!disabled && p === "codex") {
      const sandboxOptions = [
        { value: "", label: "只能寫工作區（workspace-write，預設）" },
        { value: "danger-full-access", label: "完整存取（danger-full-access）" },
        { value: "read-only", label: "唯讀（read-only）" },
      ];
      sandboxRow = el("label", { class: "row", title: "Codex 接手工作時的沙箱，討論一律唯讀；需執行 west build 等寫入工作區外快取／工具鏈的指令時請選完整存取" },
        el("span", { class: "label" }, "接手沙箱"),
        select(sandboxOptions, setting.sandbox === "workspace-write" ? "" : setting.sandbox || "", (v) => vscode.postMessage({ type: "set", party: p, field: "sandbox", value: v }), disabled));
    }

    const quota = state.quota ? state.quota[p] : null;
    return el("section", { class: `card${disabled ? " disabled" : ""}` },
      el("label", { class: "head" },
        el("input", {
          type: "checkbox", checked: setting.enabled,
          onchange: (e) => vscode.postMessage({ type: "toggle", party: p, enabled: e.target.checked }),
        }),
        el("span", { class: "name" }, names[p]),
        disabled ? el("span", { class: "badge" }, "未參與") : null),
      quotaBar(quota, (state.quota && state.quota.threshold_pct) || 5),
      el("label", { class: "row" }, el("span", { class: "label" }, "模型"), modelSelect),
      effortRow,
      shareRow,
      subagentRow,
      sandboxRow,
      cat.error ? el("div", { class: "error" }, `⚠ ${cat.error}`) : null);
  }

  // 版本：Agora 核心（agora --version）與擴充套件；核心以 pip install -e 安裝時會隨程式更新，擴充套件要重新打包安裝
  function versionLine(v) {
    const ext = `擴充套件 v${v.extension}`;
    if (!state.workspace || !state.agoraFound) return el("p", { class: "muted small version" }, ext);
    if (!v.core) return el("p", { class: "muted small version" }, `Agora 核心版本：查詢中… · ${ext}`);
    const text = v.core === v.extension ? `Agora v${v.core}` : `Agora 核心 v${v.core} · ${ext}`;
    return el("p", { class: `muted small version${v.core === v.extension ? "" : " mismatch"}` }, text,
      v.core === v.extension ? null : el("span", { class: "hint" }, "（版本不一致，請重新打包並安裝擴充套件）"));
  }

  const SUBAGENT_HINTS = {
    claude: "被 Agora 呼叫時能否用 Task 派出 Claude Code 的內建子代理（general-purpose、Explore 等）。關閉時直接禁用該工具。",
    antigravity: "被 Agora 呼叫時能否用 invoke_subagent 等工具派出 Antigravity 的子代理。關閉時由專案裡的 hook 擋下（需已安裝或更新到此專案）。",
    codex: "被 Agora 呼叫時能否用 spawn_agent 派出 Codex 的子代理。Codex 目前無法強制關閉：關閉時只在提示中要求不要使用，若仍使用會標記在逐字稿。",
  };
  const SUBAGENT_OFF = { claude: "已禁用", antigravity: "由 hook 擋下", codex: "只能提示，無法強制" };

  function render() {
    if (!state) return;
    const children = [];
    if (!state.workspace) {
      children.push(el("p", { class: "muted" }, "請先開啟一個資料夾。"));
    } else if (!state.agoraFound) {
      children.push(el("div", { class: "notice" }, "找不到 Agora。請執行 pip install -e <Agora 資料夾>，或在設定中指定 agora.root。",
        el("button", { onclick: () => vscode.postMessage({ type: "settings" }) }, "開啟設定")));
    } else {
      const install = state.install;
      if (install && install.state !== "installed") {
        const text = {
          outdated: `此專案的 Agora 項目是舊版（${install.outdated.join("、")}），更新後 AI 才會使用目前版本的指令。`,
          partial: `此專案的 Agora 只裝了一部分（缺少：${install.missing.join("、")}），AI 可能不知道可以透過 Agora 找其他 AI。`,
          missing: "此專案尚未安裝 Agora：AI 不知道可以透過 Agora 找其他 AI 討論，也沒有額度提醒。先選好參與的 AI，再安裝（只會安裝所選 AI 的 skills、hook 與權限）。",
        }[install.state];
        children.push(el("div", { class: "notice" }, text,
          el("button", { onclick: () => vscode.postMessage({ type: "install" }) },
            install.state === "outdated" ? "更新" : "安裝到此專案")));
      }
      if (state.errors.parties) children.push(el("div", { class: "error" }, state.errors.parties));
      if (state.parties) {
        children.push(...state.order.map(partyCard));
      } else if (!state.errors.parties) {
        children.push(el("p", { class: "muted" }, "載入中…"));
      }
      if (state.errors.catalog) children.push(el("div", { class: "error" }, `模型清單：${state.errors.catalog}`));
      if (state.errors.quota) children.push(el("div", { class: "error" }, `額度：${state.errors.quota}`));
      children.push(el("div", { class: "actions" },
        el("button", { onclick: () => vscode.postMessage({ type: "newThread" }) }, "新增討論串"),
        el("button", { class: "secondary", onclick: () => vscode.postMessage({ type: "refreshQuota" }) }, "更新額度"),
        el("button", { class: "secondary", onclick: () => vscode.postMessage({ type: "refreshModels" }) }, "更新模型清單")));
      children.push(el("p", { class: "muted small" },
        "勾選表示同意該 AI 參與此專案接下來的討論與分派：進行中的討論串在下一次輪到發言時就會加入它（先讀完前面的討論），"
        + "取消勾選後不再請它回覆；已在背景進行的分派工作不會換人，用「新增討論串」時指定參與者的討論串也不會自動加入。"
        + "設定存在 .agora/config.json；未參與的 AI 不會被呼叫，也不會成為接手方。"
        + "分配比例只影響自動挑選接手方（依近期接手輪數補足落後的一方），指定接手方時不受限制。"));
    }
    children.push(versionLine(state.versions || {}));
    app.replaceChildren(...children);
  }

  window.addEventListener("message", (e) => {
    if (e.data.type === "state") {
      state = e.data.state;
      render();
    }
  });
  vscode.postMessage({ type: "ready" });
})();
