---
name: agora
description: 透過 Agora 與 Antigravity、Codex 進行多輪技術討論、分派工作，或接續其他 AI 發起的討論串。當使用者要求「跟 Antigravity／Codex 討論」「問問其他 AI」「讓你們討論」「分工完成」或提到 Agora 討論串時使用。
---

# Agora：與 Antigravity、Codex 討論與分工

工具：`python D:/github/agora/agora.py`（說明：D:/github/agora/README.md）。討論串在工作區的 `.agora/threads/<id>/`，
`transcript.md` 是給使用者看的逐字稿。工作區預設為目前資料夾的 git 根目錄。

## 發起討論

1. 先自己讀相關程式碼，整理出立場與依據，不要把未查證的說法丟給別人。
2. 建立討論串並送出開場（預設為此專案啟用的 AI，見 `agora.py parties`；`--parties claude,codex` 可再縮小範圍）：

```bash
T=$(python D:/github/agora/agora.py new "主題")
python D:/github/agora/agora.py send "$T" --from claude <<'EOF'
開場：背景、你的立場、具體想請對方回應的問題（引用 檔案:行號）
EOF
```

`send --from claude` 預設依序請其他所有 AI 回覆；`--to codex` 只請 Codex。每次呼叫可能需要數分鐘，Bash timeout 設 600000。
討論在背景程序執行：等待被中斷（逾時、視窗關閉）時討論仍會繼續，之後用 `python D:/github/agora/agora.py wait <id>` 接上並取得回覆，`activity` 查看所有進行中的討論與分派。

3. 查證回覆中的論點後再回應。預設最多 4 來回；出現「【已達成共識】」或只剩立場差異時停止。

## 分派工作

討論出分工後，把工作單（範圍、不能碰的檔案、驗收方式）寫成檔案再分派：

```bash
python D:/github/agora/agora.py assign "$T" --from claude --to codex --file 工作單.md
python D:/github/agora/agora.py status "$T" --wait 900     # 等它完成（可用 run_in_background）
```

接手方在背景執行，只能在範圍內改檔、不能 commit。完成後**逐項審查它的 git diff**、跑驗證，再由你 commit。

## 規則

- 你代表這個互動中的 Claude Code，用 `--from claude` 發言。不要用 `auto` 代替自己思考，除非使用者要求「讓它們自己討論」。
- 討論期間不改程式碼；結束後向使用者回報：共識、分歧（各方理由）、建議的下一步、transcript.md 路徑。
- 其他 AI 的說法一律先查證；它們也可能改到範圍外，審查時要看 git diff。
