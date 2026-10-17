"""ローカル LLM（Ollama 等の OpenAI 互換 API）で動くツール呼び出しエージェント。

- 既定は Ollama: http://localhost:11434/v1 / モデルは環境変数 ACCOUNT_KEEPER_MODEL
- 登録情報はローカルから一切出ない（クラウド LLM を使わない前提）
- LLM に接続できないときは --offline でキーワードルーティングのモック動作
"""
from __future__ import annotations

import json
import os
import urllib.request

from .tools import build_tools

SYSTEM = (
    "あなたは Account Keeper。ユーザーが各Webサービスに登録した情報（メール・電話・住所ラベル・2FA・"
    "支払いカードの愛称）を管理するアシスタントです。必ずツールで事実を確認してから答え、"
    "重要度の高い順に、次に取るべき行動を短く日本語で提案してください。"
    "パスワード・カード番号・秘密の質問の答えは絶対に扱わず、求められたらパスワードマネージャーを案内します。"
)


class LocalAgent:
    def __init__(self, store, base_url: str | None = None, model: str | None = None, max_steps: int = 6):
        self.tools = build_tools(store)
        self.base_url = (base_url or os.environ.get("ACCOUNT_KEEPER_LLM_URL", "http://localhost:11434/v1")).rstrip("/")
        self.model = model or os.environ.get("ACCOUNT_KEEPER_MODEL", "qwen3:8b")
        self.max_steps = max_steps

    def _schema(self) -> list[dict]:
        return [{"type": "function", "function": {"name": n, "description": spec["description"],
                                                  "parameters": spec["inputSchema"]}}
                for n, (spec, _) in self.tools.items()]

    def _chat(self, messages: list[dict]) -> dict:
        body = json.dumps({"model": self.model, "messages": messages, "tools": self._schema()}).encode()
        req = urllib.request.Request(f"{self.base_url}/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read())["choices"][0]["message"]

    def run(self, question: str, verbose: bool = False) -> str:
        messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": question}]
        for _ in range(self.max_steps):
            msg = self._chat(messages)
            calls = msg.get("tool_calls") or []
            messages.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")})
            if not calls:
                return msg.get("content") or ""
            for c in calls:
                name = c["function"]["name"]
                args = json.loads(c["function"].get("arguments") or "{}")
                if verbose:
                    print(f"  → {name}({args})")
                try:
                    result = self.tools[name][1](**args)
                except Exception as e:
                    result = {"error": str(e)}
                messages.append({"role": "tool", "tool_call_id": c.get("id", name),
                                 "content": json.dumps(result, ensure_ascii=False)})
        return "（ステップ上限に達しました）"


def offline_answer(store, question: str) -> str:
    """LLM なしのモック。キーワードでツールを選び、結果を整形するだけ。"""
    tools = build_tools(store)
    q = question.lower()
    if "2fa" in q or "二段階" in q or "認証" in q:
        rows = tools["audit_2fa"][1]()
        return "\n".join(f"[{r['severity']}] {r['service']}: {r['two_factor']} → {r['suggestion']}" for r in rows) or "問題なし"
    if "退会" in q or "使って" in q or "放置" in q:
        rows = tools["find_dormant"][1]()
        return "\n".join(f"{r['service']}: {r['days_unused']}日未使用 → {r['suggestion']}" for r in rows) or "該当なし"
    if "@" in q:
        email = next(w for w in question.split() if "@" in w)
        rows = tools["plan_change"][1]("email", email)
        return f"{email} から変更する場合の更新対象 {len(rows)} 件:\n" + "\n".join(
            f"- {r['service']} ({r['category']})" for r in rows)
    if "メール" in q:
        rows = tools["email_exposure"][1]()
        return "\n".join(f"{r['email']}: {r['count']}サービス" for r in rows)
    return "オフラインモードでは「2FA」「退会」「メール」「<メールアドレス> を変えたい」に対応しています。"
