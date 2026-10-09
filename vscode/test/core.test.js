const test = require("node:test");
const assert = require("node:assert");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { syncCore } = require("../core");

function makeBundle(dir, hash, content = "print('agora')") {
  fs.mkdirSync(path.join(dir, "agoralib"), { recursive: true });
  fs.writeFileSync(path.join(dir, "agora.py"), content);
  fs.writeFileSync(path.join(dir, "agoralib", "__init__.py"), '__version__ = "9.9.9"');
  fs.writeFileSync(path.join(dir, "BUILD.json"), JSON.stringify({ version: "9.9.9", hash }));
}

test("同步內建核心到固定位置，內容相同時不重複複製，更新時換新", () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "agora-core-"));
  try {
    const bundled = path.join(tmp, "ext", "core");
    const dest = path.join(tmp, "home", ".agora", "core");
    makeBundle(bundled, "aaa");
    assert.strictEqual(syncCore(bundled, dest), dest);
    assert.ok(fs.existsSync(path.join(dest, "agoralib", "__init__.py")));

    fs.writeFileSync(path.join(dest, "marker"), "x");          // 雜湊相同：不重新複製
    syncCore(bundled, dest);
    assert.ok(fs.existsSync(path.join(dest, "marker")));

    makeBundle(bundled, "bbb", "print('new')");               // 新版：整份換新
    syncCore(bundled, dest);
    assert.ok(!fs.existsSync(path.join(dest, "marker")));
    assert.strictEqual(fs.readFileSync(path.join(dest, "agora.py"), "utf8"), "print('new')");
    assert.deepStrictEqual(fs.readdirSync(path.dirname(dest)), ["core"]);   // 沒有留下暫存資料夾
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});

test("沒有內建核心時回傳 undefined", () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "agora-core-"));
  try {
    assert.strictEqual(syncCore(path.join(tmp, "none"), path.join(tmp, "dest")), undefined);
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});
