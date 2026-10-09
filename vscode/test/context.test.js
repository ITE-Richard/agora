// npm test（node --test "test/*.test.js"）
const test = require("node:test");
const assert = require("node:assert");
const { tooLarge, fence, pickDiagnostics, formatContext, MAX_DIAGNOSTICS } = require("../context");

const diag = (line, severity, message = `m${line}`) => ({ line, endLine: line, column: 1, severity, source: "py", message });

test("範圍內的診斷優先，並設上限", () => {
  const all = [diag(1, 0), diag(50, 1), diag(12, 1), diag(11, 0)];
  const picked = pickDiagnostics(all, { start: 10, end: 20 }, 3);
  assert.deepStrictEqual(picked.shown.map((d) => d.line), [11, 12, 1]);
  assert.strictEqual(picked.inside, 2);
  assert.strictEqual(picked.omitted, 1);
});

test("診斷數量上限", () => {
  const many = Array.from({ length: 30 }, (_, i) => diag(i + 1, 0));
  assert.strictEqual(pickDiagnostics(many, null).shown.length, MAX_DIAGNOSTICS);
});

test("fence 比程式碼內的反引號長", () => {
  assert.strictEqual(fence("a"), "```");
  assert.strictEqual(fence("x ```` y"), "`````");
});

test("格式包含路徑、行號、版本、未儲存提示與診斷", () => {
  const text = formatContext({
    relPath: "src/app.py", languageId: "python", version: 7, dirty: true,
    range: { start: 10, end: 12 }, code: "x = 1\n", lineCount: 100, diagnostics: [diag(11, 0, "undefined name")],
  });
  assert.match(text, /檔案：src\/app\.py:10-12（python，版本 7，尚未儲存/);
  assert.match(text, /```python\nx = 1\n```/);
  assert.match(text, /- src\/app\.py:11:1 錯誤 \[py\] undefined name/);
});

test("不附程式碼與沒有診斷", () => {
  const text = formatContext({
    relPath: "big.py", languageId: "python", version: 1, dirty: false, range: null, code: null, lineCount: 5000,
    diagnostics: [],
  });
  assert.match(text, /整份檔案，5000 行/);
  assert.match(text, /未附程式碼內容/);
  assert.match(text, /診斷：無/);
});

test("檔案大小門檻", () => {
  assert.strictEqual(tooLarge(100, 1000), false);
  assert.strictEqual(tooLarge(401, 1000), true);
  assert.strictEqual(tooLarge(10, 20001), true);
});
