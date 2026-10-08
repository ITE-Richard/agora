<!-- agora:begin -->
## Agora：與 Claude Code、Antigravity 討論與分工

當使用者要求「跟 Claude／Antigravity 討論」「問問其他 AI」「讓你們討論」「分工完成」或提到 Agora 討論串時：

- 工具：`python D:/github/agora/agora.py`（說明：D:/github/agora/README.md）。討論串在 `.agora/threads/<id>/`，`transcript.md` 給使用者看。
- 發起：先讀相關程式碼整理立場，再 `python D:/github/agora/agora.py new "主題"`，
  接著 `python D:/github/agora/agora.py send <id> --from codex --file 開場.md`（預設依序請其他 AI 回覆；`--to claude` 只請一方）。
  討論在背景程序執行：等待被中斷（逾時、視窗關閉）時討論仍會繼續，之後用 `python D:/github/agora/agora.py wait <id>` 接上並取得回覆，`activity` 查看所有進行中的討論與分派。
- 分派：`python D:/github/agora/agora.py assign <id> --from codex --to claude --file 工作單.md`，
  再 `python D:/github/agora/agora.py status <id> --wait 900`；完成後審查 git diff 並跑驗證。
- 你用 `--from codex` 發言；其他 AI 的說法先查證；討論期間不改程式碼；結束時向使用者回報共識、分歧與下一步。
- 若你是被 Agora 呼叫來回覆的（環境變數 AGORA_INVOKED=1），直接回覆即可，不要再執行討論或分派指令。
<!-- agora:end -->
