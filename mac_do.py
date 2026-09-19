#!/usr/bin/env python
"""mac-do — 自然言語でmacOSアプリのUI要素をクリックする(完全ローカル)。

設計(2026-08-27実証、10_Labs/2026-08-27-local-gui-agent-qwen38-grounding-e2e.md):
  座標 = macOS AX API(決定論・正確) / 意味選択 = chakuho(GX10) / 実行 = CGEvent。
  ピクセル接地(VLM)は使わない。AXが薄いアプリは対象外(将来のフォールバック候補)。

意味選択バックエンド(2026-09-19、gx10-claudeとの契約確定):
  第一候補 = chakuho(GX10 vLLM qwen3.8-27b、1トークンlogprob判定)。接続先は環境変数CHAKUHO_URL。
  52件超はchakuho側が内部トーナメントで吸収するため、mac-do側の事前絞り込みは不要(上限2704)。
  chakuho不通時は既定では降格しない(判定不能でexit非0)。--fallback-ollamaを付けた時だけ
  旧来のOllama qwen3.6ベース(llm_select)へ降格する(黙って降格すると不通に誰も気づけないため。
  fleet design-first規約、mini指揮経由2026-09-19指摘)。

使い方:
  mac-do "日記のフォルダを開いて"                 # 前面アプリに対して実行
  mac-do --app Obsidian "設定を開いて"            # アプリ指定(前面化してから実行)
  mac-do --dry-run "..."                          # 選択結果の表示のみ(クリックしない)
  mac-do --list --app Obsidian                    # 要素一覧を出すだけ
  mac-do --fallback-ollama "..."                  # chakuho不通時にOllamaへ降格を許可

安全装置(二段構え、DANGEROUS正規表現が常にauthoritative):
  - 破壊的な語(削除/Delete/ゴミ箱/Remove/初期化 等)を含む要素は --force なしでは押さない
  - 上記に引っかからなくてもchakuhoのrisk判定が「安全」以外、またはrisk判定自体が
    低confidence(coverage<0.5)の場合は--dry-run相当に落として--forceを要求する
  - target選択のcoverageが低い(<0.5)場合は「判定不能」として押さずに終了する
  - chakuho不通は既定では「判定不能」として押さない(--fallback-ollamaで明示許可)
  - LLM(旧経路)の空応答は1回だけ自動リトライ(癖カタログ: 6回中1回空応答の実測)
"""

from __future__ import annotations
import argparse, json, os, re, subprocess, sys, time, urllib.error, urllib.request

import ApplicationServices as AS
import Quartz

OLLAMA = "http://localhost:11434/api/chat"
MODEL = "qwen3.6:35b-a3b-coding-nvfp4"
CHAKUHO_URL = os.environ.get("CHAKUHO_URL", "http://localhost:9750/v1/systemone")
COVERAGE_MIN = 0.5
RISK_LEVELS = ["安全", "要注意", "破壊的"]
MAX_NODES = 6000
MAX_DEPTH = 40
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "events.log")
DANGEROUS = re.compile(
    r"削除|消去|初期化|ゴミ箱|破棄|空にする|フォーマット|"
    r"Delete|Remove|Trash|Recycle|Erase|Reset|Uninstall|Format|Empty|Discard",
    re.I,
)


def log_event(text: str):
    """events.logへ1行追記する(書けなくても本処理は止めない)。"""
    try:
        with open(LOG_PATH, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {text}\n")
    except OSError:
        pass


def ax_attr(el, name):
    err, val = AS.AXUIElementCopyAttributeValue(el, name, None)
    return val if err == 0 else None


def frontmost_app_name() -> str:
    out = subprocess.run(
        [
            "osascript",
            "-e",
            'tell application "System Events" to get name of first process whose frontmost is true',
        ],
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def enumerate_elements(app_name: str) -> list[dict]:
    pids = subprocess.run(
        ["pgrep", "-x", app_name], capture_output=True, text=True
    ).stdout.split()
    if not pids:
        sys.exit(f"mac-do: アプリ '{app_name}' が起動していない")
    app = AS.AXUIElementCreateApplication(int(pids[0]))
    # Electron系はAX木を明示要求しないと公開しない(2026-08-27実証)
    try:
        AS.AXUIElementSetAttributeValue(app, "AXManualAccessibility", True)
    except Exception:
        pass
    out, seen = [], [0]

    def walk(el, depth):
        if seen[0] >= MAX_NODES or depth > MAX_DEPTH:
            return
        seen[0] += 1
        role = str(ax_attr(el, "AXRole") or "")
        title = ax_attr(el, "AXTitle") or ""
        desc = ax_attr(el, "AXDescription") or ""
        value = ax_attr(el, "AXValue")
        label = title or desc or (str(value)[:60] if isinstance(value, str) else "")
        pos, size = ax_attr(el, "AXPosition"), ax_attr(el, "AXSize")
        if label and pos is not None and size is not None:
            okp, p = AS.AXValueGetValue(pos, AS.kAXValueCGPointType, None)
            oks, s = AS.AXValueGetValue(size, AS.kAXValueCGSizeType, None)
            if okp and oks and s.width > 1 and s.height > 1:
                out.append(
                    {
                        "id": len(out),
                        "role": role,
                        "label": str(label)[:80],
                        "cx": int(p.x + s.width / 2),
                        "cy": int(p.y + s.height / 2),
                        "w": int(s.width),
                        "h": int(s.height),
                    }
                )
        for c in ax_attr(el, "AXChildren") or []:
            walk(c, depth + 1)

    walk(app, 0)
    return out


class ChakuhoUnavailable(Exception):
    """chakuhoへの到達失敗(不通・タイムアウト・不正応答)。呼び出し側は既定では降格せず判定不能とする。"""


def chakuho_select(instruction: str, elements: list[dict]) -> dict:
    """chakuho(GX10)へ対象選択+risk判定を問う。失敗時はChakuhoUnavailableを送出する。"""
    criteria = {str(e["id"]): f"[{e['role']}] {e['label']}" for e in elements}
    criteria["__none__"] = "どの要素も指示に合わない(該当なし)"
    body = json.dumps(
        {
            "state": {
                "elements": [
                    {"id": e["id"], "role": e["role"], "label": e["label"]}
                    for e in elements
                ]
            },
            "questions": {
                "target": {
                    "type": "choice",
                    "instructions": f"目標: {instruction}。操作する要素を1つ選ぶ",
                    "criteria": criteria,
                },
                "risk": {
                    "type": "score",
                    "instructions": "選ばれた要素を操作する危険度",
                    "criteria": RISK_LEVELS,
                },
            },
        }
    ).encode()
    req = urllib.request.Request(
        CHAKUHO_URL, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as ex:
        raise ChakuhoUnavailable(f"{type(ex).__name__}: {ex} (接続先 {CHAKUHO_URL})") from ex


def llm_select(instruction: str, elements: list[dict]) -> int | None:
    listing = "\n".join(f"{e['id']}: [{e['role']}] {e['label']}" for e in elements)
    prompt = (
        f"以下はmacOSアプリの画面上のUI要素一覧です。ユーザーの指示:「{instruction}」。"
        f'最も適切な要素のidを1つ、JSONで {{"id": int}} のみ出力してください。'
        f'該当が無ければ {{"id": -1}} と出力。\n\n{listing}'
    )
    body = json.dumps(
        {
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "think": False,
            "options": {"num_predict": 200},
        }
    ).encode()
    for attempt in range(2):  # 空応答1リトライ(実測: 6回中1回)
        req = urllib.request.Request(
            OLLAMA, data=body, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=300) as r:
            d = json.load(r)
        content = (d.get("message", {}) or {}).get("content", "") or ""
        m = re.search(r'"id"\s*:\s*(-?\d+)', content)
        if m:
            return int(m.group(1))
    return None


def click(cx: int, cy: int):
    for t in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
        e = Quartz.CGEventCreateMouseEvent(None, t, (cx, cy), Quartz.kCGMouseButtonLeft)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, e)
        time.sleep(0.05)


def main():
    ap = argparse.ArgumentParser(prog="mac-do")
    ap.add_argument("instruction", nargs="?", help="自然言語の指示")
    ap.add_argument("--app", help="対象アプリ名(省略時は前面アプリ)")
    ap.add_argument("--dry-run", action="store_true", help="選択のみ、クリックしない")
    ap.add_argument("--list", action="store_true", help="要素一覧を出力して終了")
    ap.add_argument("--force", action="store_true", help="破壊的ラベルの安全装置を解除")
    ap.add_argument(
        "--fallback-ollama",
        action="store_true",
        help="chakuho不通時にOllama(旧経路)へ降格する(既定は降格せず判定不能でexit)",
    )
    a = ap.parse_args()

    app_name = a.app or frontmost_app_name()
    if a.app:
        subprocess.run(["osascript", "-e", f'tell application "{a.app}" to activate'])
        time.sleep(1.0)

    els = enumerate_elements(app_name)
    if a.list:
        for e in els:
            print(
                f"{e['id']}: [{e['role']}] {e['label']}  @({e['cx']},{e['cy']}) {e['w']}x{e['h']}"
            )
        return
    if not a.instruction:
        ap.error("指示を書いてください(または --list)")
    if not els:
        sys.exit(f"mac-do: '{app_name}' からAX要素が取れない(AXが薄いアプリの可能性)")

    risk_top, risk_uncertain = None, False
    t0 = time.time()
    try:
        result = chakuho_select(a.instruction, els)
    except ChakuhoUnavailable as ex:
        dt = time.time() - t0
        log_event(
            f"chakuho不通 fallback_ollama={a.fallback_ollama} latency={dt:.1f}s detail={ex}"
        )
        if not a.fallback_ollama:
            sys.exit(f"mac-do: 判定不能(chakuho不通: {ex})")
        print(
            f"mac-do: WARN chakuho不通のためOllamaへ降格"
            f"(--fallback-ollama指定, {dt:.1f}秒で失敗: {ex})",
            file=sys.stderr,
        )
        sel = llm_select(a.instruction, els)
        if sel is None:
            sys.exit("mac-do: LLM応答からidを抽出できない(リトライ済み)")
        if sel == -1:
            sys.exit(f"mac-do: 指示に該当する要素なし(全{len(els)}要素)")
    else:
        answers = result.get("answers", {})
        target = answers.get("target", {})
        risk = answers.get("risk", {})
        if target.get("coverage", 0) < COVERAGE_MIN:
            sys.exit(
                f"mac-do: 対象要素の確信度が低い(coverage={target.get('coverage')}) — 判定不能"
            )
        choice = target.get("choice")
        if choice == "__none__":
            sys.exit(f"mac-do: 指示に該当する要素なし(全{len(els)}要素)")
        try:
            sel = int(choice)
        except (TypeError, ValueError):
            sys.exit(f"mac-do: chakuhoの選択idが不正: {choice!r}")
        risk_probs = risk.get("probabilities") or {}
        risk_top = max(risk_probs, key=risk_probs.get) if risk_probs else None
        risk_uncertain = risk.get("coverage", 1.0) < COVERAGE_MIN

    e = next((x for x in els if x["id"] == sel), None)
    if e is None:
        sys.exit(f"mac-do: 選択されたid={sel}が存在しない")

    tag = f"[{e['role']}] {e['label']} @({e['cx']},{e['cy']})"
    if DANGEROUS.search(e["label"]) and not a.force:
        sys.exit(f"mac-do: 破壊的ラベルのため停止(--forceで解除): {tag}")
    if (
        risk_uncertain or (risk_top is not None and risk_top != "安全")
    ) and not a.force:
        reason = (
            "risk判定が不能(coverage不足)" if risk_uncertain else f"risk判定={risk_top}"
        )
        if a.dry_run:
            print(f"[dry-run] 選択: {tag} ({reason})")
            return
        sys.exit(
            f"mac-do: {reason}のため停止(--forceで解除、または--dry-runで確認): {tag}"
        )
    if a.dry_run:
        print(f"[dry-run] 選択: {tag}")
        return
    click(e["cx"], e["cy"])
    print(f"クリック実行: {tag}")


if __name__ == "__main__":
    main()
