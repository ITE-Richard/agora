# Agora

讓 VSCode 裡的 **Claude Code、Antigravity、Codex** 在同一個專案裡互相討論、分派工作、完成任務；使用者隨時可以閱讀逐字稿或插話。也負責三方的額度監控：某一方快用完時，自動把工作交接給還有額度的一方。

每個專案可以選擇讓哪幾個 AI 參與、各用哪個模型（`agora parties`，或 [VSCode 擴充套件](vscode/README.md) 的側邊欄面板）。

## 運作方式

- 每個 AI 都透過自己的非互動 CLI 被呼叫：`claude -p`、`agy -p`、`codex exec`。
- 一個**討論串**對每個 AI 各保留一段對話；有人發言時，工具把對方還沒看過的訊息一次轉過去。
- 討論串存在**目標專案**的 `.agora/threads/<id>/`：`transcript.md` 給人看，`state.json` 給程式用。
- **分派工作**時，接手方在背景執行：每完成一小步就寫 `progress.md`、檢查是否被叫停、檢查自己的額度；額度用完會等重置後繼續。
- 回覆進行中的內容（文字、讀檔與執行指令等動作）即時寫到討論串的 `live/discussion.md`、`live/work.md`，動作也會顯示在終端；完成後才正式寫進逐字稿並刪除暫存檔，中途停止的回覆不會寫入。
- 討論時若某一方用了不被允許的工具或終端指令而整輪中斷，會提醒它改用讀檔工具、接續同一段對話重試一次；仍失敗時記進逐字稿，並繼續請其他 AI 回覆。
- 接手方的工具被拒時（見[工具被拒](#工具被拒)），Agora 依被拒的種類告訴它該怎麼做，並把被拒的動作記下來；之後若仍宣告完成，`status` 會提醒分派方查證來源。
- 討論與分派都在**獨立的背景程序**執行，發起的那一方（你或某個 AI 的終端指令）只負責顯示進度：
  關掉 VSCode 視窗、終端指令逾時都不會中斷它們，結果照樣寫進逐字稿。
- 同一個討論串可以一邊討論、一邊有接手方在背景工作：等 AI 回覆期間不鎖住討論串，兩邊的訊息都會寫入，期間新增的訊息下一次再轉給該 AI。

工作區預設為目前資料夾的 git 根目錄，也可用 `--workspace` 指定。

## 安裝

只用 VSCode 的話，安裝 [VSCode 擴充套件](vscode/README.md) 即可：它內建 Agora 核心，會同步到 `~/.agora/core`，再從面板「安裝到此專案」。

要在終端機使用，或要修改 Agora 本身：

```bash
pip install -e D:/github/agora     # 提供固定的 agora 指令（可編輯安裝，直接使用這份程式）
cd <專案資料夾> && agora install    # 安裝 skills、hook、權限與 git 排除
```

路徑含空格時（例如 `C:/Users/Richard Clayd/.agora/core`），skills、hook 與權限裡的指令會自動加上引號。

- 用 `pip install -e` 安裝後，skills、接手方的指令權限與工作提示都改用固定的 `agora` 指令，Agora 搬家或更新時不必替每個專案重新 install（額度 hook 仍記錄路徑，搬家後要重新 install）。
- 沒有安裝時照樣可以用 `python <Agora 資料夾>/agora.py`；Antigravity 的權限會同時保留兩種寫法，已裝過的 skills 不受影響。
- 寫進 skills 與 `AGENTS.md` 的指令：PATH 上有 `agora` 就固定寫 `agora`，不論這次是哪一份 Agora 執行 install；PATH 上沒有時才寫完整路徑，並在 `AGENTS.md` 的區段內註明原因。
- 給接手方的工作提示與 Claude 的指令白名單，只有當 PATH 上的 `agora` 屬於目前這個 Python、而且指向這份程式時才用它，否則用路徑寫法（確保執行的是同一份程式）。

### install 會改哪些檔案

- 只有執行 install 時才會改寫工作區的檔案：終端機的 `agora install`、VSCode 面板的「安裝到此專案／更新」按鈕（Agora 升級後安裝狀態變成「舊版」，面板就會顯示「更新」），以及建立討論串時「此專案尚未安裝 Agora，要先安裝嗎？」選「安裝」。
  擴充套件啟動時同步 `~/.agora/core`、額度 hook、討論與分派都不會改寫工作區的檔案；被 Agora 呼叫的 AI（接手方）不能執行 install。
- 只改 Agora 管理的部分：skills（`.claude/skills/agora|relay`、`.agents/skills/agora|relay`）、`AGENTS.md` 的 `<!-- agora:begin -->` 區段、`.claude/settings.local.json` 與 `.agents/hooks.json` 裡 Agora 的 hook，以及全域的 `~/.gemini/antigravity-cli/settings.json` 權限。
- 內容沒有實質變化（只差換行字元或行尾空白）時不寫入；寫入時沿用檔案原本的換行字元，不會只因換行而產生 git 改動。
- install 輸出的第一行列出這次實際改寫的檔案（VSCode 的通知顯示的也是這一行），同時記進 `.agora/install.log`（時間、Agora 版本與位置、改了哪些檔案）。
- 額度 hook 記的是**執行 install 的那份 Agora** 的路徑（擴充套件內建的 `~/.agora/core`，或 `pip install -e` 的資料夾）。從不同地方執行 install 會改寫 hook 路徑，建議固定用同一種方式。
- 接手期間若有人執行 install，被改寫的 hook 在越界檢查中標成「Agora 自動更新」並保留，`changes` 也會標示，不算接手方的改動；接手方自己改的照樣還原。
  辨認依據是 install 記在 `~/.agora/install_writes.jsonl` 的內容雜湊，接手方寫不到這個位置。

## 指令

```bash
A=agora    # 或 A="python D:/github/agora/agora.py"

$A parties                                          # 此專案參與的 AI 與模型
$A parties --disable antigravity --model codex=gpt-6.1-sol --effort claude=high
$A parties --share claude=5 --share codex=3          # 自動分派的工作比例
$A parties --subagents codex=off                    # 被 Agora 呼叫時能否啟動自己的內建子代理（on / off）
$A parties --sandbox codex=danger-full-access       # Codex 接手工作時的沙箱（預設 workspace-write）
$A models [--refresh]                               # 各 AI 可選的模型與推理強度
$A new "主題" [--parties claude,codex] [--model codex=gpt-5] [--effort codex=high]
$A send <id> --from claude --message "..."          # 預設依序請其他所有 AI 回覆
$A send <id> --from claude --to codex --file msg.md # 只請 Codex
$A send <id> --from human --message "..."           # 使用者插話（只記錄，下次轉給 AI）
$A reply <id> --party antigravity                   # 不發言，直接請某方回覆
$A auto <id> --rounds 6 [--order codex,claude]      # AI 依序自動發言（各方都表示【已達成共識】時提前結束）
$A assign <id> --from claude --to codex --file 工作單.md   # 分派工作（不指定 --to 則依分配比例挑選）
$A summarize <id> --by claude                       # 用新對話讀完整逐字稿，產出共識、取捨、異議與工作單（summary.md）
$A assign <id> --from human --from-summary [--yes]  # 用總結的工作單分派；總結過期時要 --yes 確認
$A status <id> [--wait 900] [--json]                # 分派狀態與最新進度（見下方 exit code）
$A changes <id> [--json]                            # 分派期間的變更（相對於分派當下的工作區快照）
$A recall <id> [--wait [秒數]]                       # 請接手方停止；--wait 等到真的停止（預設最多 600 秒）
$A check                                            # 工作區驗證（語法檢查＋測試）
$A build                                            # 專案建置（.agora/config.json 的 build 指令）
$A quota [--refresh] [--json]                       # 三方剩餘額度
$A quota --unblock codex                            # 清除「無法使用」標記（帳務或權限問題處理好之後）
$A activity                                         # 所有進行中的討論與分派（誰正在回覆）
$A wait <id>                                        # 接上背景進行中的討論，顯示回覆直到結束
$A stop <id>                                        # 停止進行中的討論
$A list / show <id> --last 3
$A prune [--days 30] [--verbose]                    # 列出不再需要的討論串（不會刪除）
$A prune --yes [--keep-sessions]                    # 確認刪除
$A keep <id> [--off]                                # 標記保留，prune 不會清理
```

`<id>` 可以用前綴或 `latest`。沒給 `--message` / `--file` 時讀 stdin。

## 等待分派結果

`status <id> --wait N` 等到接手結束或超過 N 秒。加 `--wait` 時以 exit code 區分結果，腳本不必再解析 `state.json`：

| exit code | 意思 |
|---|---|
| 0 | 已結束，而且完成（`done`） |
| 1 | 已結束，但沒有完成（`failed`、`blocked`、`interrupted`、`stopped`、`max_rounds`），或沒有分派紀錄 |
| 2 | 等待逾時，接手方仍在進行；輸出第一行是「⏳ 等待 N 秒逾時，接手方仍在進行（狀態：running）」 |

沒加 `--wait` 時只報告狀態，exit code 一律是 0。`--json` 輸出
`{status, worker, alive, rounds, timed_out, waiting_until, updated, started, error, denied, denied_summary, warning, last_progress, exit_code}`。

- 剛 `assign` 的工作在背景程序記下 PID 前（狀態 `starting` 未滿 60 秒）一律視為進行中，`status`、`activity` 與其他同時執行的指令不會把它判成中斷。
  assign 必須先寫入狀態再啟動背景程序（背景程序一啟動就讀取它），所以用這段緩衝，而不是先啟動再寫入。
- `recall` 依當時狀態說明多久會停：等待額度時約 1 分鐘內（每分鐘檢查一次）；正在執行某一輪時，要等這一輪結束（會顯示第幾輪、已執行多久）。
  `recall --wait [秒數]` 等到真的停止：停止或完成時 exit code 0，以其他狀態結束為 1，逾時為 2。
- 不提供強制中止（`--now`）：每一輪結束時，Agora 會還原接手方改動的 hook、自動執行的設定與 `.agora/config.json`（見[權限與安全](#權限與安全)）。
  中途終止會跳過這一步，等於讓接手方繞過指令白名單；被中斷的 CLI 對話狀態也無法預期。
  真的必須立刻停止時，從工作管理員結束接手程序，再用 `agora changes <id>` 與 `git status` 檢查工作區，並手動確認 `.claude/`、`.agents/`、`.codex/`、git hooks 沒有被改動。

## 總結與依總結分派

- `summarize` 請指定的一方開**新的對話**、讀**完整逐字稿**（不只未讀訊息），依固定格式輸出：共識、採納與放棄的意見、保留的異議、`## 工作單`（範圍、步驟、驗收條件）。
  結果存成討論串的 `summary.md`，也記進逐字稿。總結方由使用者指定；它額度不足時會提示改用誰，不自動換人。
- `assign --from-summary` 只取 `## 工作單` 段落當工作單；找不到或是空的就拒絕分派。
- 總結時記下訊息數與工作單提到的檔案雜湊；之後又有新訊息或檔案變了，會提示總結可能已過期，要加 `--yes` 才分派。這只用來偵測變動，不代表誰改了什麼。

## 參與的 AI 與模型

設定存在工作區的 `.agora/config.json`（`parties` 區段），沒設定的 AI 視為參與、模型與推理強度沿用各 CLI 的預設：

- 勾選（啟用）表示同意該 AI 參與接下來的討論與分派；未參與的 AI 不會被請求回覆、不會被挑為接手方，額度 hook 也只在參與的 AI 之間提醒交接。
- 設定改了，既有討論串下一次呼叫就生效：取消勾選的 AI 不再被請求回覆；之後才勾選的 AI 在下一次輪到時加入（`auto` 進行中也會在下一輪加入），第一次發言前會收到說明與完整討論內容。
  用 `new --parties` 指定參與者的討論串固定為那幾個，不自動加入；已在背景進行的分派工作不會換接手方。
- 模型優先順序：討論串建立時的 `--model` / `--effort` ＞ 專案設定 ＞ CLI 預設。
- `install` 只安裝參與中 AI 的 skills、hook 與權限。`agora install --status` 檢查此專案缺少或過期的項目（只有 `.agora/config.json` 不算安裝；在面板調整設定就會建立它）。
- 在 git 專案裡，`install` 會把 Agora 建立的資料夾與檔案（`.agora/`、skills、`.claude/settings.local.json`，以及由它新建的 `AGENTS.md`、`.agents/hooks.json`）寫進 `.git/info/exclude` 的 Agora 區段，不改 `.gitignore`，避免被 commit。已被 git 追蹤的檔案排除無效，install 會列出來，可用 `git rm -r --cached <路徑>` 取消追蹤。

### 內建子代理

三個 AI 都有自己的內建子代理。`subagents`（預設允許）決定被 Agora 呼叫時能不能使用：

| AI | 內建子代理 | 不允許時 |
|---|---|---|
| Claude Code | `Task`（general-purpose、Explore、Plan 等） | `--disallowedTools Task`，工具直接移除 |
| Antigravity | `invoke_subagent`、`define_subagent`、`manage_subagents`、`browser_subagent` | `install` 裝在 `.agents/hooks.json` 的 PreToolUse hook 擋下（只看 Agora 傳的環境變數，你自己開的 Antigravity 不受影響）；被擋時它會改用自己的工具完成 |
| Codex | `spawn_agent` 等 | 目前版本無法強制關閉（關掉 `multi_agent` 功能後仍可使用），只在提示中要求不要使用；若仍使用，逐字稿的附註會標記 |

### 工作分配比例

`share`（預設 1）決定**自動挑選接手方**時各 AI 分到的工作量：`assign` 沒指定 `--to`、以及額度不足時 hook 提醒交接給誰。
明確指定接手方時不受比例限制；`0` 表示只參與討論、不自動分派給它。

- 工作量以**接手輪數**計：接手方每跑完一輪記一筆在 `.agora/work_log.jsonl`，統計最近 7 天（`AGORA_SHARE_DAYS`）；進行中的分派先算 1 輪。
- 每次挑「再分一輪後占比仍最落後」的一方，也就是 `(輪數 + 1) / 比例` 最小者；額度低於門檻或被標記無法使用的跳過，大家都不足時退回挑額度最多的（仍不挑無法使用的）。
- 例如 Claude 5、Codex 3、Antigravity 2，十輪工作大約分成 5 / 3 / 2。

| AI | 模型清單來源 | 推理強度 |
|---|---|---|
| Claude Code | 官方別名 fable / opus / sonnet / haiku（也可輸入完整模型名稱） | `--effort` |
| Antigravity | `agy models` | 已包含在模型名稱（例如 `gemini-3.1-pro-high`） |
| Codex | `~/.codex/models_cache.json`（沒有時用 `codex debug models`） | `model_reasoning_effort`，依模型支援的等級 |

## 額度

| AI | 來源 | 數字代表什麼 |
|---|---|---|
| Claude Code | 用 Haiku 發一個極短請求，讀回應的 `rate_limit_event`；Agora 呼叫 Claude 時也順便更新 | 訂閱的 **5 小時**與 **7 天**使用視窗，顯示比較緊的那個 |
| Antigravity | 向執行中的 agy hub / language server（本機 loopback）呼叫 `GetUserStatus`，同 Quota Deck | 各模型的 `quotaInfo.remainingFraction` 與重置時間，依額度池（Gemini、Claude）取最低的；週期由 Antigravity 決定 |
| Codex | 讀 `~/.codex/sessions/**/rollout-*.jsonl` 最後一筆 `rate_limits`，不需額外請求 | ChatGPT 方案的 `primary`／`secondary` 速率視窗（通常是 **5 小時**與 **7 天**），顯示比較緊的那個 |

- 這些都是**速率或週期額度**，用完會在重置時間恢復。**Codex 的 workspace credits 查不到**（它不在 `rate_limits` 裡）：credits 用完時額度仍可能顯示還有剩，只有實際呼叫失敗才知道。
- Codex 的數字只在它執行過之後才會更新（讀它自己的紀錄）；很久沒用就會停在舊值。額度資料超過 2 小時（`AGORA_QUOTA_STALE`）沒更新時，顯示會改成「N 小時前，可能過期」。
- 呼叫失敗的錯誤分兩類：
  - **會自己恢復**的速率或週期額度（rate limit、429、usage limit、resets at…）：接手方等到重置時間再繼續；查不到重置時間時每 15 分鐘（`AGORA_QUOTA_BACKOFF`）重試，最多 4 次（`AGORA_QUOTA_RETRIES`），之後判定失敗。
  - **需要人處理**的帳務或權限問題（out of credits、billing、payment、insufficient_quota、subscription、workspace owner…）：不等待也不重試。接手立刻停在 `blocked`，原始錯誤記在 `error`，進度檔、逐字稿、`status`、`activity` 都顯示「需要人工處理」。
    同時把這一方標記為**無法使用**（`~/.agora/blocked.json`）：`agora quota`、hook、VSCode 都改顯示「Codex：無法使用（workspace 沒有 credits，07:04 偵測）」，自動分派與交接提醒也會跳過它。
    標記在下一次成功呼叫它時自動清除，也可以處理好後用 `agora quota --unblock codex` 手動清除。

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
| Claude Code | 禁用 Edit/Write | `acceptEdits`；Bash 只允許 `agora.py quota/check/build` 與唯讀 git；不能改 `.agora/config.json` |
| Antigravity | 沒有寫檔權限 | `--mode accept-edits`（只能寫工作區）；指令依 `~/.gemini/antigravity-cli/settings.json` 的 `permissions.allow`（完整比對） |
| Codex | `-s read-only`（不受沙箱設定影響） | `-s workspace-write`（預設，`add_dirs` 的目錄另外可寫）；可用 `agora parties --sandbox codex=danger-full-access` 改為完整存取 |

- 分派時用暫時的 git index 替工作區拍快照，存在 `refs/agora/<討論串>`（不動使用者的 index 與工作區；被忽略的檔案與 `.env`、金鑰不進快照），`changes` 與 VSCode 的「檢視分派期間的變更」以它為基準；清理討論串時一併刪除。期間任何人的修改都會列出。
- 接手方的書面界線：只改工作單範圍、不 commit/push、不碰 `.env`、不做影響外部系統的動作；每輪前後比對 `git status` 並記在逐字稿。
- hook 與會自動執行指令的設定（`.claude/settings*.json`、`.agents/hooks.json`、`.codex/config.toml`、`.vscode/tasks.json`、git hooks）改了等於繞過指令白名單：Claude 接手時直接禁止編輯 `.claude/`、`.agents/`、`.codex/`、`.git/` 與 `.vscode/tasks.json`；每輪結束時這些檔案被新增、修改或刪除都會還原，並記在逐字稿。
- `.agora/config.json` 的 `build`、`check`、`add_dirs` 與沙箱設定決定接手方能執行的指令與能寫的範圍：接手方執行的 `agora build`／`check` 用該輪開始時的指令（以環境變數 `AGORA_LOCKED_COMMANDS` 帶入），中途改設定檔也不會被執行；被改動的設定在該輪結束時還原，並記在逐字稿。
- 被 Agora 呼叫的 CLI 帶 `AGORA_INVOKED=1`，無法再呼叫 Agora，避免互相無限呼叫。
- Antigravity 的指令權限是**完整比對**，所以接手方只被允許執行固定、不帶參數的指令：`agora.py quota`、`agora.py check`、`agora.py build`、`git status/diff/log`。權限是全域設定，升級後新增的指令要重新 `install` 才會加入（安裝狀態會顯示為需要更新）。

### 工具被拒

非互動模式沒辦法詢問使用者，不在允許清單的工具會直接被拒；Antigravity 會因此整輪沒有輸出。接手方被拒時，Agora 會：

- 依被拒的種類（終端指令 `command`、讀網址 `read_url`、開網址 `execute_url`、寫檔 `write_file`、MCP 工具等）接續同一段對話，說明該怎麼做；最多重試 3 次。
  例如 `read_url` 被拒時會明說「這個網址不在允許清單；如果工作需要這份來源，請停止並回報【已暫停】，不要用記憶或其他來源替代」。
- 把每次被拒的動作（種類，以及找得到時的目標網址、指令或路徑）記在 `state.json` 的 `relay.denied`，同時寫進進度檔與逐字稿。
  `status`、`activity`、VSCode 的完成通知都會顯示「本次接手有 N 次工具被拒：read_url ×2」。
- 被拒過、之後卻宣告【工作完成】時，`status` 與完成通知會警告：結果可能沒有真的讀到來源，請查證後再採用。

要讓 Antigravity 讀取外部網頁，在 `~/.gemini/antigravity-cli/settings.json` 的 `permissions.allow` 加上網域規則，每個網域都要個別列出，不能寫 `read_url(*)`：

```json
{
  "permissions": {
    "allow": ["read_url(www.naer.edu.tw)", "read_url(cirn.moe.edu.tw)"]
  }
}
```

這是全域設定，所有專案的討論與接手都適用；`install` 不會替你加網址規則。

## 設定

| 環境變數 | 預設 |
|---|---|
| `AGORA_WORKSPACE` | 目前資料夾的 git 根目錄 |
| `AGORA_HUMAN_NAME` | Richard |
| `CLAUDE_PATH` / `AGY_PATH` / `CODEX_PATH` | 自動尋找 VSCode 擴充套件內建的執行檔 |
| `AGORA_TIMEOUT` / `AGORA_WORK_TIMEOUT` | 600 / 7200 秒 |
| `AGORA_QUOTA_THRESHOLD` | 5（%） |
| `AGORA_MAX_WORK_ROUNDS` | 12（接手方最多跑幾輪；等額度、被拒重試不算） |
| `AGORA_QUOTA_BACKOFF` | 900 秒（接手方回報額度用盡、但查不到重置時間時，等多久再試） |
| `AGORA_QUOTA_RETRIES` | 4（上述情況最多重試幾次，超過就判定失敗） |
| `AGORA_QUOTA_STALE` | 7200 秒（額度資料超過這麼久沒更新，顯示可能過期） |
| `AGORA_AGY_POOL` | gemini（Antigravity 內 Gemini 與 Claude 模型是不同額度池） |

工作區的 `.agora/config.json` 可設定：

- `summarizer`：`summarize` 沒給 `--by` 時的總結方，例如 `"claude"`。
- `build`：`agora build` 執行的建置指令，例如 `"west build"`。有設定時，接手方完成工作前也會執行。
- `check`：`agora check` 在語法檢查後執行的驗證指令，例如 `[".venv\\Scripts\\python.exe", "-m", "pytest", "-q"]`；沒設定時自動偵測（見[接手方的驗證環境](#接手方的驗證環境)）。`check` 不包含建置。
- `add_dirs`：Codex 接手工作（workspace-write）時額外可寫的目錄，相對路徑以工作區為準，不存在的略過。

`build`、`check` 寫成字串時交給 shell 執行；寫成字串清單時直接執行、不經 shell；寫成清單的清單時依序執行，遇到失敗就停。
參與的 AI、模型與 Codex 的沙箱模式可用 `agora parties` 或 VSCode 面板調整。

### 接手方的驗證環境

接手方受指令白名單限制，只能執行不帶參數的 `agora check` 與 `agora build`，不能自己改跑 `.venv\Scripts\python -m pytest`。
所以測試在哪個 Python、用什麼工具跑，完全由 `.agora/config.json` 的 `check`／`build` 決定；接手當輪用的是該輪開始時的設定，接手方改了也不會生效。

沒有設定 `check` 時，`agora check` 自動偵測：

1. Python：工作區有 `.venv` 或 `venv`（Windows 為 `Scripts\python.exe`，其他平台為 `bin/python`）就用它，否則用 Agora 自己的 Python。
2. 測試工具：專案有 pytest 設定（`pyproject.toml` 的 `[tool.pytest…]`、`pytest.ini`、`conftest.py`、`setup.cfg`、`tox.ini`）、而且上面那個 Python 裝了 pytest，就執行 `python -m pytest -q`；否則有 `tests/` 才執行 `python -m unittest discover -s tests`。

`agora check` 輸出的第一行是實際執行的指令、Python 路徑與來源（設定檔、分派當輪的設定或自動偵測），環境不對時一眼就看得出來。

- 偵測不到正確環境時（例如虛擬環境在別處、要用 tox 或 nox），請明確設定 `check`。
- `install` 偵測到 `.venv`／`venv` 但沒有設定 `check`，或 `check` 用的是舊版 install 寫入的 Agora 自己的 Python 時，會印出建議的設定。新的 install 不再把 `check` 寫進設定檔。
- `build` 不會自動偵測；專案需要建置（例如產生檔案、編譯韌體）時請設定 `build`，接手方完成工作前會執行它。
- `check` 會執行專案的測試程式碼，接手方改了測試就等於改了 `check` 執行的內容；白名單限制的是「能執行哪些指令」，不是測試內容，請照常審查接手方的變更。

## 專案結構

```
agora.py               進入點
pyproject.toml         pip 套件設定（agora 指令）
agoralib/cli.py        討論、分派、狀態、驗證
agoralib/config.py     工作區設定（參與的 AI、模型、推理強度、分配比例）
agoralib/shares.py     依分配比例挑選接手方、記錄接手輪數
agoralib/snapshot.py   分派基準快照與分派期間的變更
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
