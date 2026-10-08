# Agora

讓 VSCode 裡的 **Claude Code、Antigravity、Codex** 在同一個專案裡互相討論、分派工作、完成任務；使用者隨時可以閱讀逐字稿或插話。也負責三方的額度監控：某一方快用完時，自動把工作交接給還有額度的一方。

每個專案可以選擇讓哪幾個 AI 參與、各用哪個模型（`agora parties`，或 [VSCode 擴充套件](vscode/README.md) 的側邊欄面板）。

## 運作方式

- 每個 AI 都透過自己的非互動 CLI 被呼叫：`claude -p`、`agy -p`、`codex exec`。
- 一個**討論串**對每個 AI 各保留一段對話；有人發言時，工具把對方還沒看過的訊息一次轉過去。
- 討論串存在**目標專案**的 `.agora/threads/<id>/`：`transcript.md` 給人看，`state.json` 給程式用。
- **分派工作**時，接手方在背景執行：每完成一小步就寫 `progress.md`、檢查是否被叫停、檢查自己的額度；額度用完會等重置後繼續。
- 討論與分派都在**獨立的背景程序**執行，發起的那一方（你或某個 AI 的終端指令）只負責顯示進度：
  關掉 VSCode 視窗、終端指令逾時都不會中斷它們，結果照樣寫進逐字稿。

工作區預設為目前資料夾的 git 根目錄，也可用 `--workspace` 指定。

## 指令

```bash
A="python D:/github/agora/agora.py"

$A parties                                          # 此專案參與的 AI 與模型
$A parties --disable antigravity --model codex=gpt-6.1-sol --effort claude=high
$A parties --share claude=5 --share codex=3          # 自動分派的工作比例
$A models [--refresh]                               # 各 AI 可選的模型與推理強度
$A new "主題" [--parties claude,codex] [--model codex=gpt-5] [--effort codex=high]
$A send <id> --from claude --message "..."          # 預設依序請其他所有 AI 回覆
$A send <id> --from claude --to codex --file msg.md # 只請 Codex
$A send <id> --from human --message "..."           # 使用者插話（只記錄，下次轉給 AI）
$A reply <id> --party antigravity                   # 不發言，直接請某方回覆
$A auto <id> --rounds 6 [--order codex,claude]      # AI 依序自動發言
$A assign <id> --from claude --to codex --file 工作單.md   # 分派工作（不指定 --to 則依分配比例挑選）
$A status <id> [--wait 900]                         # 分派狀態與最新進度
$A recall <id>                                      # 請接手方在下一個檢查點暫停
$A check                                            # 工作區驗證（語法檢查＋測試）
$A quota [--refresh] [--json]                       # 三方剩餘額度
$A activity                                         # 所有進行中的討論與分派（誰正在回覆）
$A wait <id>                                        # 接上背景進行中的討論，顯示回覆直到結束
$A stop <id>                                        # 停止進行中的討論
$A list / show <id> --last 3
$A prune [--days 30] [--verbose]                    # 列出不再需要的討論串（不會刪除）
$A prune --yes [--keep-sessions]                    # 確認刪除
$A keep <id> [--off]                                # 標記保留，prune 不會清理
```

`<id>` 可以用前綴或 `latest`。沒給 `--message` / `--file` 時讀 stdin。

## 參與的 AI 與模型

設定存在工作區的 `.agora/config.json`（`parties` 區段），沒設定的 AI 視為參與、模型與推理強度沿用各 CLI 的預設：

- 未參與的 AI 不會被請求回覆、不會被挑為接手方；額度 hook 也只在參與的 AI 之間提醒交接。設定改了，既有討論串下一次呼叫就生效。
- 模型優先順序：討論串建立時的 `--model` / `--effort` ＞ 專案設定 ＞ CLI 預設。
- `install` 只安裝參與中 AI 的 skills、hook 與權限。

### 工作分配比例

`share`（預設 1）決定**自動挑選接手方**時各 AI 分到的工作量：`assign` 沒指定 `--to`、以及額度不足時 hook 提醒交接給誰。
明確指定接手方時不受比例限制；`0` 表示只參與討論、不自動分派給它。

- 工作量以**接手輪數**計：接手方每跑完一輪記一筆在 `.agora/work_log.jsonl`，統計最近 7 天（`AGORA_SHARE_DAYS`）；進行中的分派先算 1 輪。
- 每次挑「再分一輪後占比仍最落後」的一方，也就是 `(輪數 + 1) / 比例` 最小者；額度低於門檻的跳過，大家都不足時退回挑額度最多的。
- 例如 Claude 5、Codex 3、Antigravity 2，十輪工作大約分成 5 / 3 / 2。

| AI | 模型清單來源 | 推理強度 |
|---|---|---|
| Claude Code | 官方別名 fable / opus / sonnet / haiku（也可輸入完整模型名稱） | `--effort` |
| Antigravity | `agy models` | 已包含在模型名稱（例如 `gemini-3.1-pro-high`） |
| Codex | `~/.codex/models_cache.json`（沒有時用 `codex debug models`） | `model_reasoning_effort`，依模型支援的等級 |

## 額度

| AI | 來源 |
|---|---|
| Claude Code | 用 Haiku 發一個極短請求，讀回應的 `rate_limit_event`；Agora 呼叫 Claude 時也順便更新 |
| Antigravity | 向執行中的 agy hub / language server（本機 loopback）呼叫 `GetUserStatus`，同 Quota Deck |
| Codex | 讀 `~/.codex/sessions/**/rollout-*.jsonl` 最後一筆 `rate_limits`，不需額外請求 |

快取在 `~/.agora/`。`agoralib/quota_hook.py` 是給 Claude Code（UserPromptSubmit、PostToolUse）與 Antigravity（PreInvocation）用的 hook：每次附上參與中 AI 的額度，自己低於 5% 時提醒把工作交給額度最多的一方。

## 清理

討論串與各 CLI 的對話紀錄不會自動刪除。`prune` 預設只列出，加 `--yes` 才刪除。
一個討論串要**同時符合**以下條件才會被清理（`--thread <id>` 指定時不看天數，其餘照樣檢查）：

- 超過 `--days` 天（預設 30）沒有任何活動
- 沒有標記保留（`keep`）
- 沒有分派工作，或分派的工作已完成；暫停、失敗、等待中的都保留
- 目前沒有人在使用

刪除討論串時，會一併刪除 Agora 為它在各 CLI 開的對話紀錄（`--keep-sessions` 可保留）；使用者用 `--session` 帶進來的對話不會刪：

| AI | 刪除方式 |
|---|---|
| Codex | 官方指令 `codex delete --force <id>` |
| Claude Code | `~/.claude/projects/*/<id>.jsonl`（Claude Code 本身也會在 30 天後自動清理） |
| Antigravity | `~/.gemini/antigravity-cli` 下該對話的檔案與索引；對話正在使用時略過 |

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

工作區的 `.agora/config.json` 可設定 `check` 指令，例如 `{"check": ["python", "-m", "unittest", "discover", "-s", "tests"]}`；參與的 AI 與模型見上方。

## 專案結構

```
agora.py               進入點
agoralib/cli.py        討論、分派、狀態、驗證
agoralib/config.py     工作區設定（參與的 AI、模型、推理強度、分配比例）
agoralib/shares.py     依分配比例挑選接手方、記錄接手輪數
agoralib/prune.py      清理不再需要的討論串與對話紀錄
agoralib/jobs.py       背景執行（關閉視窗不中斷）、進度轉送
agoralib/models.py     各 AI 可選的模型
agoralib/parties.py    三方 CLI 呼叫
agoralib/quota.py      三方額度
agoralib/quota_hook.py 額度提醒 hook
agoralib/statusline.py Claude Code status line（選用）
skills/                各 AI 的使用說明（claude、antigravity 的 skill；codex 的 AGENTS.md 片段）
vscode/                VSCode 擴充套件（側邊欄：AI 與模型、討論串；狀態列額度）
```
