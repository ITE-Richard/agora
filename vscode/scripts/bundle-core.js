// 打包前把 Agora 核心（agora.py、agoralib、skills）複製進擴充套件的 core/，並寫入 BUILD.json（版本與內容雜湊）。
// 安裝後擴充套件會把它同步到 ~/.agora/core（見 core.js），不依賴打包者電腦上的路徑。
const crypto = require("crypto");
const fs = require("fs");
const path = require("path");

const repo = path.resolve(__dirname, "..", "..");
const dest = path.resolve(__dirname, "..", "core");
const ENTRIES = ["agora.py", "agoralib", "skills", "README.md"];

function listFiles(dir, base = dir) {
  const out = [];
  for (const name of fs.readdirSync(dir).sort()) {
    const full = path.join(dir, name);
    if (name === "__pycache__" || name.endsWith(".pyc")) continue;
    if (fs.statSync(full).isDirectory()) out.push(...listFiles(full, base));
    else out.push(path.relative(base, full));
  }
  return out;
}

if (!fs.existsSync(path.join(repo, "agora.py"))) {
  console.error(`找不到 ${path.join(repo, "agora.py")}`);
  process.exit(1);
}
fs.rmSync(dest, { recursive: true, force: true });
const hash = crypto.createHash("sha256");
for (const entry of ENTRIES) {
  const src = path.join(repo, entry);
  const files = fs.statSync(src).isDirectory() ? listFiles(src).map((f) => path.join(entry, f)) : [entry];
  for (const rel of files) {
    const target = path.join(dest, rel);
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.copyFileSync(path.join(repo, rel), target);
    hash.update(rel.replace(/\\/g, "/")).update(fs.readFileSync(target));
  }
}
const version = (fs.readFileSync(path.join(repo, "agoralib", "__init__.py"), "utf8").match(/__version__\s*=\s*"([^"]+)"/) || [])[1];
const build = { version, hash: hash.digest("hex").slice(0, 16) };
fs.writeFileSync(path.join(dest, "BUILD.json"), JSON.stringify(build, null, 2) + "\n");
console.log(`已打包 Agora 核心 ${build.version}（${build.hash}）→ ${dest}`);
