"""MCP サーバーとローカルエージェントで共有するツール定義。"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Callable

from . import audit
from .store import Account, Store

Tool = dict[str, Any]


def build_tools(store: Store) -> dict[str, tuple[Tool, Callable[..., Any]]]:
    def list_accounts(category: str = "") -> list[dict]:
        accs = store.all()
        if category:
            accs = [a for a in accs if a.category == category]
        return [asdict(a) for a in accs]

    def search_accounts(query: str) -> list[dict]:
        return [asdict(a) for a in store.search(query)]

    def upsert_account(**fields) -> dict:
        existing = store.get(fields.get("service", ""))
        base = asdict(existing) if existing else {}
        base.update({k: v for k, v in fields.items() if v not in (None, "")})
        return asdict(store.upsert(Account.from_dict(base)))

    def audit_2fa() -> list[dict]:
        return audit.weak_2fa(store.all())

    def find_dormant(months: int = 12) -> list[dict]:
        return audit.dormant(store.all(), months=months)

    def plan_change(field: str, old_value: str) -> list[dict]:
        return audit.change_impact(store.all(), field, old_value)

    def email_exposure() -> list[dict]:
        return audit.email_exposure(store.all())

    def s(props: dict, required: list[str] | None = None) -> dict:
        return {"type": "object", "properties": props, "required": required or []}

    str_ = {"type": "string"}
    return {
        "list_accounts": ({"description": "登録済みサービス一覧。category で絞り込み可（shopping/sns/finance/gov/work/other）",
                           "inputSchema": s({"category": str_})}, list_accounts),
        "search_accounts": ({"description": "サービス名・メール・タグなどで横断検索",
                             "inputSchema": s({"query": str_}, ["query"])}, search_accounts),
        "upsert_account": ({"description": "サービスの登録情報を追加・更新。パスワードやカード番号は受け付けない",
                            "inputSchema": s({"service": str_, "category": str_, "login_id": str_, "email": str_,
                                              "phone": str_, "address_label": str_, "two_factor": str_,
                                              "payment_ref": str_, "last_used": str_, "notes": str_},
                                             ["service"])}, upsert_account),
        "audit_2fa": ({"description": "2FA が弱い（なし/SMS/メール）アカウントを重要度順に返す",
                       "inputSchema": s({})}, audit_2fa),
        "find_dormant": ({"description": "長期間未使用のアカウント（退会候補）",
                          "inputSchema": s({"months": {"type": "integer"}})}, find_dormant),
        "plan_change": ({"description": "メール/電話/住所ラベル/カード愛称を変更するとき、更新が必要なサービスを重要度順に返す",
                         "inputSchema": s({"field": {"type": "string",
                                                     "enum": ["email", "phone", "address_label", "payment_ref"]},
                                           "old_value": str_}, ["field", "old_value"])}, plan_change),
        "email_exposure": ({"description": "メールアドレスごとの紐づきサービス数（漏えい時の影響範囲）",
                            "inputSchema": s({})}, email_exposure),
    }
