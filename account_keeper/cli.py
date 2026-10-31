"""ak コマンド。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from . import audit, retire
from .agent import LocalAgent, offline_answer
from .store import DEFAULT_DB, Store

SAMPLE = Path(__file__).resolve().parent.parent / "data" / "sample_accounts.json"


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="ak", description="Account Keeper Agent (mock)")
    p.add_argument("--db", default=os.environ.get("ACCOUNT_KEEPER_DB", str(DEFAULT_DB)))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("demo", help="架空のサンプルデータを読み込む")
    imp = sub.add_parser("import", help="JSON から読み込む"); imp.add_argument("file")
    bw = sub.add_parser("import-bitwarden", help="Bitwarden 書き出し CSV（項目名と URL のみ。パスワード列は読まない）")
    bw.add_argument("file")
    sub.add_parser("list")
    sub.add_parser("audit", help="2FA・休眠・メール集中をまとめて点検")
    ret = sub.add_parser("retire", help="古いアカウントの洗い出しと退会・連携解除の手順書（実行は持ち主）")
    ret.add_argument("--months", type=int, default=12); ret.add_argument("--out", default="")
    ret.add_argument("--email", action="append", default=[], help="Outlook 等で維持する登録メール（複数可）")
    ch = sub.add_parser("change", help="登録情報変更の影響範囲")
    ch.add_argument("field", choices=["email", "phone", "address_label", "payment_ref"]); ch.add_argument("old")
    ask = sub.add_parser("ask", help="ローカル LLM エージェントに質問")
    ask.add_argument("question"); ask.add_argument("--offline", action="store_true"); ask.add_argument("-v", action="store_true")
    a = p.parse_args(argv)

    store = Store(a.db)
    accs = store.all()
    if a.cmd == "demo":
        print(f"{store.import_json(SAMPLE)} 件のサンプルを読み込みました → {a.db}")
    elif a.cmd == "import":
        print(f"{store.import_json(a.file)} 件読み込みました")
    elif a.cmd == "import-bitwarden":
        stats: dict = {}
        n = retire.import_bitwarden_csv(store, a.file, stats=stats)
        store.secure()  # 登録情報 DB は所有者のみ読み書き
        print(f"{n} 行（項目名と URL のみ）→ 新規 {stats['created']} 件・既存補完 {stats['updated']} 件"
              f"・同名マージ {stats['merged']} 件 → {a.db}")
    elif a.cmd == "list":
        for x in accs:
            print(f"{x.service:<22} {x.category:<9} {x.email:<26} 2FA:{x.two_factor:<8} last:{x.last_used}")
    elif a.cmd == "audit":
        print("■ サマリ", json.dumps(audit.summary(accs), ensure_ascii=False))
        print("\n■ 2FA が弱い")
        for r in audit.weak_2fa(accs):
            print(f"  [{r['severity']:<6}] {r['service']:<22} {r['two_factor']:<6} → {r['suggestion']}")
        print("\n■ 休眠アカウント（12ヶ月以上）")
        for r in audit.dormant(accs):
            print(f"  {r['service']:<22} {r['days_unused']}日 → {r['suggestion']}")
        print("\n■ メールアドレスごとの紐づき")
        for r in audit.email_exposure(accs):
            print(f"  {r['email']:<28} {r['count']}件")
    elif a.cmd == "change":
        rows = audit.change_impact(accs, a.field, a.old)
        print(f"{a.field}={a.old} を変更する場合、更新が必要なサービス {len(rows)} 件（重要度順）")
        for r in rows:
            print(f"  - {r['service']:<22} {r['category']:<9} {r['note']}")
    elif a.cmd == "retire":
        if a.months < 1:
            p.error("--months は 1 以上にしてください（0 以下だと使用中のサービスまで退会候補になります）")
        cands = retire.candidates(accs, months=a.months, primary_emails=a.email)
        text = retire.render_manual(cands, accs, months=a.months)
        if a.out:
            try:
                written = retire.write_manual(text, a.out)  # 0600 で保存（手順書に秘密情報なし）
            except (ValueError, OSError) as e:
                p.error(f"手順書を書き出せません: {e}")
            print(f"手順書を書き出しました → {written}")
        else:
            print(text)
    elif a.cmd == "ask":
        if a.offline:
            print(offline_answer(store, a.question))
        else:
            print(LocalAgent(store).run(a.question, verbose=a.v))


if __name__ == "__main__":
    main()
