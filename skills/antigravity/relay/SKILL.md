---
name: relay
description: 額度快用完時把工作交接給還有額度的 AI（Claude Code 或 Codex）、回來後收回並同步。當額度提醒（【額度】或 ⚠️ 額度低於 5%）出現、使用者要求「交接給其他 AI」「收回工作」時使用。
---

# Relay：額度交接

每次對話開始時，hook 會注入一行【額度】（Claude、Antigravity、Codex 的剩餘量與重置時間）；
額度低於 5% 時會加上 ⚠️ 指示。隨時可執行 `python D:/github/agora/agora.py quota` 查詢。

## A. 自己不足、其他 AI 足夠：交接

1. 停下目前的小步驟，不要開始新的大改動。
2. 寫交接單（存成暫存檔），要讓沒有上下文的人也能接手：
   - 目標與目前進度（做完什麼、還剩什麼，依序列出）
   - **範圍**：允許修改的檔案或資料夾；明確寫出不能碰的部分
   - 關鍵決定與理由、已知的坑
   - 如何驗證（要跑的測試指令）
3. 交接（若目前沒有相關討論串，先 `python D:/github/agora/agora.py new "主題"`）：
   ```powershell
   python D:/github/agora/agora.py assign <thread> --from antigravity --file <交接單路徑>
   ```
4. 你無法自己排程喚醒，所以要告訴 Richard：交接給 Claude、討論串路徑、你的額度幾點重置，請他屆時叫你依本 skill 的 C 段收回。然後停止工作。

## B. 所有 AI 都不足

- 若有其他 AI 比你早重置：照 A 段交接（接手程序會等 Claude 重置後才開始）。
- 否則：把目前進度寫進討論串（`python D:/github/agora/agora.py send <thread> --from antigravity --to none --file <進度檔>`），告訴 Richard 你的重置時間，然後停止。

## C. 回來後收回

1. `python D:/github/agora/agora.py recall <thread>`
2. `python D:/github/agora/agora.py status <thread> --wait 900`，等接手方停在檢查點。
3. 讀 `.agora/threads/<thread>/progress.md` 和 `git diff`，**逐項查證**接手方的改動：
   - 有沒有超出交接範圍
   - 測試是否通過
4. 向 Richard 簡短回報接手期間的進度與問題，再從進度檔的「下一步」繼續。

## 注意

- 接手方的界線：只改交接範圍、不 commit/push、不碰 .env、不跑 main.py 或下單。
- 狀態 `failed`：讀 `.agora/threads/<thread>/worker.log` 找原因，回報 Richard。
