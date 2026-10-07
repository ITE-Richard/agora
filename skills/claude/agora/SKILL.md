---
name: agora
description: 與 Antigravity（Google 的 agent）透過 Agora 工具進行多輪技術討論，或接續 Antigravity 發起的討論串。當使用者要求「跟 Antigravity 討論」「問問 Antigravity」「讓你們討論」或提到 Agora 討論串時使用。
---

# Agora：與 Antigravity 討論

工具：`python tools/agora/agora.py`（說明見 [tools/agora/README.md](../../../tools/agora/README.md)）。
每個討論串在 `discussions/<id>/`，`transcript.md` 是給 Richard 看的逐字稿。

## 發起新討論

1. 先自己讀相關程式碼，整理出立場與依據，不要把未查證的說法丟給對方。
2. 建立討論串並送出開場：

```bash
T=$(python tools/agora/agora.py new "主題")
python tools/agora/agora.py send "$T" --from claude <<'EOF'
開場內容：背景、你的立場、具體想請對方回應的問題（引用 檔案:行號）
EOF
```

3. 指令會直接印出 Antigravity 的回覆。查證回覆中的論點後，再用 `send "$T" --from claude` 回應。
4. 重複直到收斂。預設最多 4 來回，除非使用者另有指定。對方寫出「【已達成共識】」，或雙方只剩立場差異、沒有新論點時就停止。

## 接續 Antigravity 發起的討論

`python tools/agora/agora.py list` 找到討論串，`show <id>` 讀完整內容，再用 `send <id> --from claude` 回覆。

## 規則

- 你代表的是這個互動中的 Claude Code，用 `--from claude` 發言。不要用 `auto` 指令代替自己思考；`auto` 會叫起另一個無狀態的 Claude 程序，只在使用者要求「讓它們自己討論」時使用。
- 討論期間不修改程式碼；需要修改時，結束討論後先向使用者回報再動手。
- 每次呼叫要等對方回覆，可能需要 10 秒到數分鐘，Bash timeout 設 600000。
- 結束後向使用者回報：共識、仍有分歧的點（雙方各自理由）、建議的下一步，並附上 transcript.md 的路徑。
