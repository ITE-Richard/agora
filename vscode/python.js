// 找出執行 Agora 核心用的 Python。
// Windows 上的 python 常常是 Microsoft Store 的佔位程式（WindowsApps\python.exe，執行時回傳 9009），
// 而且 VSCode 從環境變數過舊的 Explorer 啟動時，PATH 裡可能根本沒有真正的 Python；所以依序找：
// 設定 agora.pythonPath → PATH 上的真 Python → 常見安裝位置 → py 啟動器。

const fs = require("fs");
const path = require("path");

/** Microsoft Store 的佔位程式：沒有安裝 Python 時會開商店或回傳 9009 */
function isStoreStub(file) {
  return /[\\/]WindowsApps[\\/]/i.test(file);
}

function exists(file, fsx) {
  try {
    return fsx.statSync(file).isFile();
  } catch {
    return false;
  }
}

/** 各 Python 3 安裝資料夾裡的 python.exe，版本新的在前（Python313 > Python312 > Python39） */
function installedPythons(roots, fsx, p) {
  const found = [];
  for (const root of roots) {
    let names = [];
    try {
      names = fsx.readdirSync(root);
    } catch {
      continue;
    }
    for (const name of names) {
      const m = /^Python3(\d+)/i.exec(name);
      const file = p.join(root, name, "python.exe");
      if (m && exists(file, fsx)) found.push({ file, minor: Number(m[1]) });
    }
  }
  return found.sort((a, b) => b.minor - a.minor).map((x) => x.file);
}

/**
 * 回傳 { cmd, args }：cmd 是要執行的程式，args 要放在 agora.py 之前（py 啟動器需要 -3）。
 * configured 是設定 agora.pythonPath；留空或 "python" 表示自動尋找。
 */
function findPython({ configured, env = process.env, platform = process.platform, fsx = fs } = {}) {
  if (configured && configured !== "python") return { cmd: configured, args: [] };
  const win = platform === "win32";
  const p = win ? path.win32 : path.posix;
  const names = win ? ["python.exe"] : ["python3", "python"];
  const dirs = (env.PATH || env.Path || "").split(win ? ";" : ":").filter(Boolean);
  for (const dir of dirs) {
    for (const name of names) {
      const file = p.join(dir, name);
      if (exists(file, fsx) && !(win && isStoreStub(file))) return { cmd: file, args: [] };
    }
  }
  if (win) {
    const roots = [env.LOCALAPPDATA && p.join(env.LOCALAPPDATA, "Programs", "Python"), env.ProgramFiles, env["ProgramFiles(x86)"]]
      .filter(Boolean);
    const [installed] = installedPythons(roots, fsx, p);
    if (installed) return { cmd: installed, args: [] };
    const launchers = [env.LOCALAPPDATA && p.join(env.LOCALAPPDATA, "Programs", "Python", "Launcher", "py.exe"),
      env.SystemRoot && p.join(env.SystemRoot, "py.exe")].filter(Boolean);
    const launcher = launchers.find((f) => exists(f, fsx));
    if (launcher) return { cmd: launcher, args: ["-3"] };
  }
  return { cmd: configured || "python", args: [] };
}

/** 執行 Python 失敗時的說明（9009 是 Windows 找不到指令，或執行到 Microsoft Store 的佔位程式；
 * 112 是 py 啟動器找不到已安裝的 Python） */
function missingPythonMessage(code, stderr) {
  // 112：py 啟動器還在、但 Python 已移除（"No installed Python found!"）
  if (code === 9009 || code === 112
    || /Python was not found|No installed Python found|No suitable Python runtime|找不到 Python/i.test(stderr || "")) {
    return "找不到可用的 Python（執行到 Microsoft Store 的佔位程式、py 啟動器找不到已安裝的 Python，或指令不存在）。請安裝 Python 3.9 以上，"
      + "或在設定 agora.pythonPath 指定 python.exe 的完整路徑；剛安裝或改過 PATH 的話，請完全關閉 VSCode 再開啟。";
  }
  return undefined;
}

module.exports = { findPython, isStoreStub, missingPythonMessage };
