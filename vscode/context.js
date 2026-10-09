// 編輯器上下文：把選取範圍（或整份檔案）的位置、內容與診斷整理成一段文字，附在訊息後面。
// 只處理純資料，不依賴 vscode 模組（方便測試）；從編輯器取資料在 extension.js。

const MAX_DIAGNOSTICS = 10;
const MAX_FILE_LINES = 400;
const MAX_FILE_CHARS = 20000;
const SEVERITY = ["錯誤", "警告", "資訊", "提示"];

/** 沒有選取時，整份檔案是否大到不該直接附上（要請使用者選取範圍，不默默截斷） */
function tooLarge(lineCount, charCount) {
  return lineCount > MAX_FILE_LINES || charCount > MAX_FILE_CHARS;
}

/** 比程式碼裡最長的連續反引號再多一個，避免程式碼提早結束 code block */
function fence(code) {
  const longest = Math.max(0, ...(code.match(/`+/g) || []).map((r) => r.length));
  return "`".repeat(Math.max(3, longest + 1));
}

/**
 * 診斷以範圍內優先（再依嚴重程度、行號），最多 limit 筆
 * diagnostics: [{ line, endLine, column, severity, source, message }]（行號從 1 起算）
 * range: { start, end } 或 null（整份檔案）
 */
function pickDiagnostics(diagnostics, range, limit = MAX_DIAGNOSTICS) {
  const inRange = (d) => !range || (d.line <= range.end && (d.endLine || d.line) >= range.start);
  const order = (a, b) => a.severity - b.severity || a.line - b.line;
  const inside = diagnostics.filter(inRange).sort(order);
  const outside = diagnostics.filter((d) => !inRange(d)).sort(order);
  const shown = [...inside, ...outside].slice(0, limit);
  return { shown, inside: inside.length, outside: outside.length, omitted: diagnostics.length - shown.length };
}

/**
 * info: { relPath, languageId, version, dirty, range: {start, end} | null, code: string | null,
 *         lineCount, diagnostics }
 * code 為 null 表示不附程式碼（檔案太大、使用者選擇只附路徑與診斷）
 */
function formatContext(info) {
  const where = info.range ? `${info.relPath}:${info.range.start}-${info.range.end}` : `${info.relPath}（整份檔案，${info.lineCount} 行）`;
  const meta = [info.languageId, `版本 ${info.version}`];
  if (info.dirty) meta.push("尚未儲存：磁碟上的內容可能和下面不同");
  const lines = ["【編輯器上下文】", `檔案：${where}（${meta.join("，")}）`];
  if (info.code != null) {
    const f = fence(info.code);
    lines.push(`${f}${info.languageId || ""}`, info.code.replace(/\s+$/, ""), f);
  } else {
    lines.push("（未附程式碼內容，請自行讀檔）");
  }
  const picked = pickDiagnostics(info.diagnostics || [], info.range);
  if (picked.shown.length) {
    const scope = info.range ? `範圍內 ${picked.inside} 筆、範圍外 ${picked.outside} 筆` : `共 ${picked.inside} 筆`;
    lines.push(`診斷（${scope}${picked.omitted ? `，只列出前 ${picked.shown.length} 筆` : ""}）：`);
    for (const d of picked.shown) {
      const source = d.source ? ` [${d.source}]` : "";
      lines.push(`- ${info.relPath}:${d.line}:${d.column} ${SEVERITY[d.severity] || "診斷"}${source} ${d.message}`);
    }
  } else {
    lines.push("診斷：無");
  }
  return lines.join("\n");
}

module.exports = { MAX_DIAGNOSTICS, tooLarge, fence, pickDiagnostics, formatContext };
