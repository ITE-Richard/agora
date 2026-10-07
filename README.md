# Agora：Claude Code ⇄ Antigravity 討論與額度交接

讓 Claude Code 與 Antigravity 在同一個工作區互相發起討論，並在一方額度快用完時把工作交接給另一方。Richard 可以隨時閱讀或插話。

## 檔案

| 檔案 | 用途 |
|---|---|
| `agora.py` | 討論、交接、收回 |
| `quota.py` | 查詢兩方剩餘額度並快取 |
| `quota_hook.py` | 兩邊 agent 的 hook：注入額度、低於 5% 時提醒交接 |
| `../usage/statusline.py` | Claude Code status line（若環境支援，額外提供 Claude 用量快照） |
| `.claude/skills/agora`、`.agents/skills/agora` | 討論流程（兩邊都能 `/agora`） |
| `.claude/skills/relay`、`.agents/skills/relay` | 交接流程（兩邊都能 `/relay`） |

## 討論

```bash
python tools/agora/agora.py new "主題"                                   # 輸出討論串 ID
python tools/agora/agora.py send <id> --from claude --message "..."     # 預設由另一個 AI 回覆
python tools/agora/agora.py send <id> --from richard --message "..."    # 只記錄，下次轉給 AI
python tools/agora/agora.py reply <id> --party antigravity               # 不發言，直接請某方回覆
python tools/agora/agora.py auto <id> --rounds 4                         # 兩個 AI 自動輪流回覆
python tools/agora/agora.py list / show <id> --last 3
```

每個討論串各保留一段 Claude session 與一段 Antigravity conversation，每次只轉送對方沒看過的訊息。`<id>` 可用前綴或 `latest`；沒給 `--message` / `--file` 時讀 stdin。

## 額度

```bash
python tools/agora/quota.py            # 兩方剩餘額度與重置時間
python tools/agora/quota.py --refresh  # 強制重新查詢
```

- **Claude**：用 Haiku 發一個不帶工具的極短請求，從回應的 `rate_limit_event` 取得 5 小時與 7 天視窗的用量（每次約 $0.01 等值，算在訂閱額度內）；Agora 呼叫背景 Claude 時也會順便更新。
- **Antigravity**：仿照 Quota Deck，向執行中的 agy hub / language server（本機 loopback）呼叫 `GetUserStatus`。需要至少有一個 Antigravity 程序在執行。Antigravity 內 Gemini 與 Claude 模型是獨立額度池，預設以 Gemini 判斷（`AGORA_AGY_POOL` 可改）。
- 快取在 `~/.agora/`；hook 只讀快取，過期時在背景更新（額度低於 20% 時每 3 分鐘，否則每 10 分鐘）。

## 交接

```bash
python tools/agora/agora.py relay <id> --from claude --file handoff.md   # 交接給另一方，背景執行
python tools/agora/agora.py relay-status <id> [--wait 900]
python tools/agora/agora.py recall <id>                                  # 請接手方在下一個檢查點暫停
```

接手方在背景執行，每完成一小步就：寫進 `discussions/<id>/progress.md`、檢查 `control.json` 是否被叫停、執行 `quota.py` 檢查自己的額度。

- 接手方額度不足 → 等到它的重置時間後自動繼續；途中被 `recall` 就停止。
- 結束標記：`【工作完成】`、`【已暫停】`、`【額度不足】`。最多 12 輪（`AGORA_MAX_WORK_ROUNDS`）。
- 喚醒：Claude 交接後用 CronCreate 在自己重置後喚醒並 `recall`（VSCode 需保持開啟）；另外開了 `autoContinueAtUsageLimit`，撞到限制時會在重置後自動繼續。Antigravity 無法自己排程，交接時會請 Richard 屆時叫它收回。

## 權限與安全

| | 討論模式 | 交接（接手）模式 |
|---|---|---|
| Claude | 禁用 Edit/Write | `acceptEdits`；Bash 只允許 unittest、py_compile、quota.py、git status/diff/log；禁讀寫 `.env` |
| Antigravity | 依 `~/.gemini/antigravity-cli/settings.json` | 同左：允許寫入 `D:\github\Taixu`，指令只允許上述幾項，其餘自動拒絕 |

- 兩種模式都有書面界線（不 commit/push、不碰 .env、不跑 main.py 或下單），每輪前後比對 `git status` 並記錄在逐字稿。
- 被 Agora 叫起來的 CLI 帶 `AGORA_INVOKED=1`，無法再呼叫 Agora；額度探測帶 `AGORA_PROBE=1` 並停用 hooks，避免互相觸發。
- 同一討論串有檔案鎖。

## 設定

| 環境變數 | 預設 |
|---|---|
| `CLAUDE_PATH` | VSCode 擴充套件中最新版的 `claude.exe` |
| `AGY_PATH` | `~/.gemini/bin/agy.exe` |
| `AGORA_TIMEOUT` / `AGORA_WORK_TIMEOUT` | 600 / 7200 秒 |
| `AGORA_QUOTA_THRESHOLD` | 5（%） |
| `AGORA_AGY_POOL` | gemini |

Claude 端的 hook 與 `autoContinueAtUsageLimit` 設在 `.claude/settings.local.json`（不進 git）；Antigravity 端的 hook 在 `.agents/hooks.json`。
