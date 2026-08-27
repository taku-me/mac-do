# mac-do — 自然言語でMacのGUIを操作する(完全ローカル)

「日記のフォルダを開いて」のような日本語1行で、macOSアプリのUI要素をクリックする。
LLMはローカル(qwen3.6/ollama)のみ、外部送信なし。

## 設計(2026-08-27実証)

**座標=macOS AX API(決定論・誤差ゼロ) / 意味選択=qwen3.6(あいまい語OK) / 実行=CGEvent。**
ピクセル接地(VLM)は使わない——実験でy座標の1024正方形規約・密リスト1行ズレ等の问题を
確認し、AXハイブリッドへ確定した。経緯: Obsidian `10_Labs/2026-08-27-local-gui-agent-qwen38-grounding-e2e.md`

## 使い方

```bash
mac-do "日記のフォルダを開いて"            # 前面アプリに実行
mac-do --app Obsidian "設定を開いて"       # アプリ指定
mac-do --dry-run "..."                     # 選択のみ表示
mac-do --list --app Obsidian               # AX要素一覧
mac-do --force "..."                       # 破壊的ラベル安全装置の解除
```

ランチャー: `~/bin/mac-do`(このrepoの.venvを叩くだけ)

## 検証済み(2026-08-27、全て実出力)

1. --list: Obsidian(Electron)から233要素列挙(AXManualAccessibility強制有効化)
2. あいまい意味選択:「日記のフォルダ」→`03_Diary`正答(ラベルに「日記」は無い)
3. 実クリック: 選択+開閉が実画面で反応
4. 安全装置: 「ゴミ箱を開いて」→`Recycle bin`を検出し停止(exit 1)。※初版のガードは
   Recycleを見逃しており検証4自身が穴を発見→パターン拡充済み
5. 該当なし: 架空ボタン指示→qwenがid:-1を返し綺麗に終了

## Gotchas / 制約

- **要Accessibility権限**(CGEventPostは権限なしだと無音で捨てられる。
  `CGPreflightPostEventAccess()`で事前確認可)
- Electronアプリは`AXManualAccessibility=True`をセットしないとAX木が空
- AXが薄いアプリ(ラベル無しcanvas描画)は不可→将来のピクセル版フォールバック候補
  (qwen3.8 + y×(H/1024)補正、ラボノート参照)。ブラウザ内WebはChrome MCPの領分
- クリックのみ(v0.1)。入力・ドラッグ・複数手順は未実装
- qwen空応答は1リトライ内蔵(実測6回中1回)
