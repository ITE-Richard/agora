# Agora for VSCode

在 VSCode 側邊欄管理 Agora：

- **AI 與模型**：勾選這個專案要讓哪些 AI（Claude Code、Antigravity、Codex）參與討論與接手工作，為每個 AI 選模型、推理強度與工作分配比例；同時顯示各自的剩餘額度。設定存在 `<專案>/.agora/config.json`，與 `agora parties` 指令共用。
- **討論串**：列出專案的討論串與分派狀態；可新增討論串、發言、讓 AI 自動討論、分派工作、查看進度、收回。點一下開啟逐字稿預覽，AI 回覆時會即時更新。
- **清理**：標題列的垃圾桶列出不再需要的討論串（閒置過久、未標記保留、沒有未完成的分派），勾選確認後刪除，並一併刪除各 AI CLI 的對話紀錄；右鍵可標記保留或刪除單一討論串。
- **進行中**：討論串顯示誰正在回覆（例如「Codex 回覆中 3 分鐘」），狀態列顯示進行中的討論與分派數量，完成或中斷時通知。
  討論在背景執行，關閉視窗也會繼續；進度通知按取消只是停止等待，要停止討論請在討論串上按右鍵「停止討論」。
- **狀態列**：顯示參與中 AI 的剩餘額度，低於門檻時變色。

所有動作都透過 `agora.py` 執行，擴充套件本身不保存任何設定。

## 安裝

```bash
cd D:/github/agora/vscode
npm run package                       # 產生 agora-0.1.0.vsix，並記錄 Agora 程式的位置
code --install-extension agora-0.1.0.vsix
```

Agora 程式搬家時，重新打包或在設定 `agora.root` 指定新位置。

## 設定

| 設定 | 預設 | 說明 |
|---|---|---|
| `agora.root` | 打包時記錄的位置 | 含 `agora.py` 的資料夾 |
| `agora.pythonPath` | `python` | 執行 agora.py 的 Python |
| `agora.quotaRefreshMinutes` | 10 | 狀態列額度更新間隔，0 表示不自動更新 |

多資料夾工作區只會使用第一個資料夾。
