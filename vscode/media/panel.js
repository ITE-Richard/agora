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
      cat.error ? el("div", { class: "error" }, `⚠ ${cat.error}`) : null);
  }

  function render() {
    if (!state) return;
    const children = [];
    if (!state.workspace) {
      children.push(el("p", { class: "muted" }, "請先開啟一個資料夾。"));
    } else if (!state.agoraFound) {
      children.push(el("div", { class: "notice" }, "找不到 Agora。請執行 pip install -e <Agora 資料夾>，或在設定中指定 agora.root。",
        el("button", { onclick: () => vscode.postMessage({ type: "settings" }) }, "開啟設定")));
    } else {
      if (!state.installed) {
        children.push(el("div", { class: "notice" },
          "此專案尚未安裝 Agora。先選好參與的 AI，再安裝（只會安裝所選 AI 的 skills、hook 與權限）。",
          el("button", { onclick: () => vscode.postMessage({ type: "install" }) }, "安裝到此專案")));
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
        "設定存在 .agora/config.json，對此專案的所有討論串生效；未參與的 AI 不會被呼叫，也不會成為接手方。"
        + "分配比例只影響自動挑選接手方（依近期接手輪數補足落後的一方），指定接手方時不受限制。"));
    }
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
