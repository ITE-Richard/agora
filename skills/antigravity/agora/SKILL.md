---
name: agora
description: 透過 Agora 與 Claude Code、Codex 進行多輪技術討論、分派工作，或接續其他 AI 發起的討論串。當使用者要求「跟 Claude／Codex 討論」「問問其他 AI」「讓你們討論」「分工完成」或提到 Agora 討論串時使用。
---

# Agora：與 Claude Code、Codex 討論與分工

工具：`python D:/github/agora/agora.py`（說明：D:/github/agora/README.md）。討論串在工作區的 `.agora/threads/<id>/`，
`transcript.md` 是給使用者看的逐字稿。

## 發起討論

1. 先自己讀相關程式碼，整理出立場與依據。
2. 建立討論串並送出開場（PowerShell；訊息較長時先寫成檔案改用 `--file`）：

```powershell
$T = python D:/github/agora/agora.py new "主題"
python D:/github/agora/agora.py send $T --from antigravity --file 開場.md
```

`send --from antigravity` 預設依序請其他所有 AI 回覆；`--to claude` 只請 Claude。

3. 查證回覆後再回應；出現「【已達成共識】」或只剩立場差異時停止。

## 分派工作

```powershell
python D:/github/agora/agora.py assign $T --from antigravity --to claude --file 工作單.md
python D:/github/agora/agora.py status $T --wait 900
```

完成後逐項審查接手方的 git diff 並跑驗證。

## 規則

- 用 `--from antigravity` 發言。討論期間不改程式碼；結束後向使用者回報共識、分歧與下一步。
- 其他 AI 的說法一律先查證。
