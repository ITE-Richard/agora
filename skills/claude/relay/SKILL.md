---
name: relay
description: 額度快用完時把工作交接給 Antigravity、重置後收回並同步。當額度提醒（【額度】或 ⚠️ 額度低於 5%）出現、使用者要求「交接給 Antigravity」「收回工作」，或排程喚醒提示要求執行交接收回時使用。
---

# Relay：額度交接

每次使用者送出訊息，hook 會附上一行【額度】（Claude 與 Antigravity 的剩餘量與重置時間）。
額度低於 5% 時，hook 會加上 ⚠️ 指示。隨時可用 `python D:/github/agora/agora.py quota` 查詢。

## A. 自己不足、Antigravity 足夠：交接

1. 停下目前的小步驟，不要開始新的大改動。
2. 寫交接單（存成暫存檔），要讓沒有上下文的人也能接手：
   - 目標與目前進度（做完什麼、還剩什麼，依序列出）
   - **範圍**：允許修改的檔案或資料夾；明確寫出不能碰的部分
   - 關鍵決定與理由、已知的坑
   - 如何驗證（要跑的測試指令）
3. 交接（若目前沒有相關討論串，先 `new` 一個）：
   ```bash
   python D:/github/agora/agora.py relay <thread> --from claude --file <交接單路徑>
   ```
4. 安排自己在重置後喚醒：用 CronCreate 建立一次性排程，時間設在 Claude 重置時間後 3 分鐘，prompt 寫：
   `額度已重置：依 relay skill 的 C 段收回 <thread> 的工作並繼續。`
5. 告訴使用者：交接給誰、討論串路徑、預計幾點回來。然後結束這一輪，不要再呼叫其他工具。

## B. 雙方都不足

- 若 Antigravity 比 Claude 早重置：照 A 段交接（接手程序會等 Antigravity 重置後才開始），並排程自己的喚醒。
- 否則：把目前進度寫進討論串（`send <thread> --from claude --to none`），排程自己的喚醒，然後停止。

## C. 喚醒後收回

1. `python D:/github/agora/agora.py recall <thread>`
2. `python D:/github/agora/agora.py status <thread> --wait 900`（Bash timeout 設 960000），等接手方停在檢查點。
3. 讀 `.agora/threads/<thread>/progress.md` 和 `git diff`，**逐項查證**接手方的改動：
   - 有沒有超出交接範圍
   - 測試是否通過
4. 向使用者簡短回報接手期間的進度與發現的問題，再從進度檔的「下一步」繼續原本的工作。

## 注意

- 接手方的界線寫在 `D:/github/agora/agoralib/cli.py` 的 WORK_PREAMBLE：只改工作單範圍、不 commit/push、不碰 .env、不做影響外部系統的動作。
- 狀態 `failed`：讀 `.agora/threads/<thread>/worker.log` 找原因，回報使用者。
- 不要用 relay 處理實盤交易或任何不可逆的操作。
