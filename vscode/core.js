// 內建的 Agora 核心：擴充套件附帶 core/（打包時由 scripts/bundle-core.js 產生），啟動時同步到 ~/.agora/core。
// 用固定位置而不是擴充套件資料夾（含版本號、每次更新都會變），skills 與 Antigravity 權限裡的指令路徑才不會因更新失效。
// 只依賴 fs，不依賴 vscode 模組（方便測試）。

const fs = require("fs");
const os = require("os");
const path = require("path");

function readBuild(dir) {
  try {
    return JSON.parse(fs.readFileSync(path.join(dir, "BUILD.json"), "utf8"));
  } catch {
    return null;
  }
}

/**
 * 把 bundled（擴充套件的 core/）同步到 dest（預設 ~/.agora/core），回傳可用的核心位置；沒有內建核心時回傳 undefined。
 * 內容雜湊相同就不複製；先複製到暫存資料夾再換上，換不上時（檔案被占用）沿用舊的。
 */
function syncCore(bundled, dest = path.join(os.homedir(), ".agora", "core"), log = () => {}) {
  const want = readBuild(bundled);
  if (!want || !fs.existsSync(path.join(bundled, "agora.py"))) return undefined;
  const have = readBuild(dest);
  if (have && have.hash === want.hash && fs.existsSync(path.join(dest, "agora.py"))) return dest;
  const tmp = `${dest}.tmp-${process.pid}`;
  try {
    fs.rmSync(tmp, { recursive: true, force: true });
    fs.mkdirSync(path.dirname(dest), { recursive: true });
    fs.cpSync(bundled, tmp, { recursive: true });
    fs.rmSync(dest, { recursive: true, force: true });
    fs.renameSync(tmp, dest);
    log(`已更新 Agora 核心 ${want.version} → ${dest}`);
    return dest;
  } catch (e) {
    log(`無法更新 ${dest}：${e.message}`);
    fs.rmSync(tmp, { recursive: true, force: true });
    return fs.existsSync(path.join(dest, "agora.py")) ? dest : bundled;
  }
}

module.exports = { syncCore, readBuild };
