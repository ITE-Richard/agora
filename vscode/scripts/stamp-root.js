// 打包前記錄 Agora 程式的位置（這個 repo 的根目錄），安裝後的擴充套件預設就用它
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..", "..");
if (!fs.existsSync(path.join(root, "agora.py"))) {
  console.error(`找不到 ${path.join(root, "agora.py")}`);
  process.exit(1);
}
fs.writeFileSync(path.join(__dirname, "..", "agora-root.json"), JSON.stringify({ root }, null, 2) + "\n");
console.log(`Agora 位置：${root}`);
