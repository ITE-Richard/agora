# Agora

讓 VSCode 裡的 **Claude Code、Antigravity、Codex** 在同一個專案裡互相討論、分派工作、完成任務；使用者隨時可以閱讀逐字稿或插話。也負責三方的額度監控：某一方快用完時，自動把工作交接給還有額度的一方。

## 運作方式

- 每個 AI 都透過自己的非互動 CLI 被呼叫：`claude -p`、`agy -p`、`codex exec`。
- 一個**討論串**對每個 AI 各保留一段對話；有人發言時，工具把對方還沒看過的訊息一次轉過去。
- 討論串存在**目標專案**的 `.agora/threads/<id>/`：`transcript.md` 給人看，`state.json` 給程式用。
- **分派工作**時，接手方在背景執行：每完成一小步就寫 `progress.md`、檢查是否被叫停、檢查自己的額度；額度用完會等重置後繼續。

工作區預設為目前資料夾的 git 根目錄，也可用 `--workspace` 指定。

## 指令

```bash
A="python D:/github/agora/agora.py"

$A new "主題" [--parties claude,antigravity,codex] [--model codex=gpt-5]
$A send <id> --from claude --message "..."          # 預設依序請其他所有 AI 回覆
$A send <id> --from claude --to codex --file msg.md # 只請 Codex
$A send <id> --from human --message "..."           # 使用者插話（只記錄，下次轉給 AI）
$A reply <id> --party antigravity                   # 不發言，直接請某方回覆
$A auto <id> --rounds 6 [--order codex,claude]      # AI 依序自動發言
$A assign <id> --from claude --to codex --file 工作單.md   # 分派工作（不指定 --to 則挑額度最多的）
$A status <id> [--wait 900]                         # 分派狀態與最新進度
$A recall <id>                                      # 請接手方在下一個檢查點暫停
$A check                                            # 工作區驗證（語法檢查＋測試）
$A quota [--refresh] [--json]                       # 三方剩餘額度
$A list / show <id> --last 3
```

`<id>` 可以用前綴或 `latest`。沒給 `--message` / `--file` 時讀 stdin。

## 額度

| AI | 來源 |
|---|---|
| Claude Code | 用 Haiku 發一個極短請求，讀回應的 `rate_limit_event`；Agora 呼叫 Claude 時也順便更新 |
| Antigravity | 向執行中的 agy hub / language server（本機 loopback）呼叫 `GetUserStatus`，同 Quota Deck |
| Codex | 讀 `~/.codex/sessions/**/rollout-*.jsonl` 最後一筆 `rate_limits`，不需額外請求 |

快取在 `~/.agora/`。`agoralib/quota_hook.py` 是給 Claude Code（UserPromptSubmit、PostToolUse）與 Antigravity（PreInvocation）用的 hook：每次附上三方額度，自己低於 5% 時提醒把工作交給額度最多的一方。

## 權限與安全

| | 討論 | 接手工作 |
|---|---|---|
| Claude Code | 禁用 Edit/Write | `acceptEdits`；Bash 只允許 `agora.py quota/check` 與唯讀 git |
| Antigravity | 依 `~/.gemini/antigravity-cli/settings.json` 的 `permissions.allow`（完整比對） | 同左 |
| Codex | `-s read-only` | `-s workspace-write`（只能寫工作區） |

- 接手方的書面界線：只改工作單範圍、不 commit/push、不碰 `.env`、不做影響外部系統的動作；每輪前後比對 `git status` 並記在逐字稿。
- 被 Agora 呼叫的 CLI 帶 `AGORA_INVOKED=1`，無法再呼叫 Agora，避免互相無限呼叫。
- Antigravity 的指令權限是**完整比對**，所以接手方只被允許執行固定、不帶參數的指令：`agora.py quota`、`agora.py check`、`git status/diff/log`。

## 設定

| 環境變數 | 預設 |
|---|---|
| `AGORA_WORKSPACE` | 目前資料夾的 git 根目錄 |
| `AGORA_HUMAN_NAME` | Richard |
| `CLAUDE_PATH` / `AGY_PATH` / `CODEX_PATH` | 自動尋找 VSCode 擴充套件內建的執行檔 |
| `AGORA_TIMEOUT` / `AGORA_WORK_TIMEOUT` | 600 / 7200 秒 |
| `AGORA_QUOTA_THRESHOLD` | 5（%） |
| `AGORA_AGY_POOL` | gemini（Antigravity 內 Gemini 與 Claude 模型是不同額度池） |

工作區的 `.agora/config.json` 可設定 `check` 指令，例如 `{"check": ["python", "-m", "unittest", "discover", "-s", "tests"]}`。

## 專案結構

```
agora.py               進入點
agoralib/cli.py        討論、分派、狀態、驗證
agoralib/parties.py    三方 CLI 呼叫
agoralib/quota.py      三方額度
agoralib/quota_hook.py 額度提醒 hook
agoralib/statusline.py Claude Code status line（選用）
skills/                各 AI 的使用說明（claude、antigravity 的 skill；codex 的 AGENTS.md 片段）
```
