const test = require("node:test");
const assert = require("node:assert");
const { findPython, isStoreStub, missingPythonMessage } = require("../python");

/** 假的檔案系統：files 是存在的檔案，dirs 是 {資料夾: [子項目]} */
function fakeFs(files, dirs = {}) {
  const lower = new Set(files.map((f) => f.toLowerCase()));
  return {
    statSync(f) {
      if (!lower.has(f.toLowerCase())) throw new Error("ENOENT");
      return { isFile: () => true };
    },
    readdirSync(d) {
      if (!dirs[d]) throw new Error("ENOENT");
      return dirs[d];
    },
  };
}

const LOCAL = "C:\\Users\\u\\AppData\\Local";
const STUB = `${LOCAL}\\Microsoft\\WindowsApps\\python.exe`;

test("設定了完整路徑就直接用", () => {
  assert.deepStrictEqual(findPython({ configured: "D:\\py\\python.exe", platform: "win32", env: {}, fsx: fakeFs([]) }),
    { cmd: "D:\\py\\python.exe", args: [] });
});

test("略過 Microsoft Store 的佔位程式，改用常見安裝位置中最新的版本", () => {
  const root = `${LOCAL}\\Programs\\Python`;
  const fsx = fakeFs([STUB, `${root}\\Python39\\python.exe`, `${root}\\Python312\\python.exe`],
    { [root]: ["Launcher", "Python39", "Python312"] });
  const env = { PATH: `${LOCAL}\\Microsoft\\WindowsApps;C:\\Windows`, LOCALAPPDATA: LOCAL };
  assert.deepStrictEqual(findPython({ configured: "python", platform: "win32", env, fsx }),
    { cmd: `${root}\\Python312\\python.exe`, args: [] });
});

test("PATH 上有真的 Python 時優先使用", () => {
  const real = "C:\\Tools\\Python\\python.exe";
  const fsx = fakeFs([STUB, real]);
  const env = { Path: `${LOCAL}\\Microsoft\\WindowsApps;C:\\Tools\\Python`, LOCALAPPDATA: LOCAL };
  assert.strictEqual(findPython({ platform: "win32", env, fsx }).cmd, real);
});

test("只剩 py 啟動器時加上 -3", () => {
  const launcher = `${LOCAL}\\Programs\\Python\\Launcher\\py.exe`;
  const env = { PATH: "C:\\Windows", LOCALAPPDATA: LOCAL };
  assert.deepStrictEqual(findPython({ platform: "win32", env, fsx: fakeFs([launcher]) }), { cmd: launcher, args: ["-3"] });
});

test("什麼都找不到時退回 python，並能說明 9009", () => {
  assert.deepStrictEqual(findPython({ platform: "win32", env: {}, fsx: fakeFs([]) }), { cmd: "python", args: [] });
  assert.ok(isStoreStub(STUB));
  assert.match(missingPythonMessage(9009, ""), /找不到可用的 Python/);
  assert.match(missingPythonMessage(1, "Python was not found; run without arguments"), /agora\.pythonPath/);
  assert.match(missingPythonMessage(112, "No installed Python found!"), /找不到可用的 Python/);   // py 啟動器還在、Python 已移除
  assert.strictEqual(missingPythonMessage(1, "其他錯誤"), undefined);
});
