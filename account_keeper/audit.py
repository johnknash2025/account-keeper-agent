"""登録情報の点検ロジック（LLM なしで動く決定的な部分）。

エージェントはこれらをツールとして呼び出し、結果を人間向けに説明・優先順位付けする。
"""
from __future__ import annotations

import calendar
from datetime import date, datetime

from .store import Account

HIGH_RISK_CATEGORIES = {"finance", "gov", "work"}


def _parse(d: str) -> date | None:
    try:
        return datetime.strptime(d, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _months_ago(today: date, months: int) -> date:
    """calendar month 単位で months ヶ月前の日付を返す（月末ズレを避ける）。

    months*30 的な日数近似だと、12 ヶ月 = 360日 になって、
    実暦で 1 年未満（例: 11 ヶ月 + 27 日 = 361日）が候補に入ったり、
    1 年ちょうど（365日）の扱いが揺れる。暦月で境界を決めることで、
    「実際に使っているサービス」を休眠候補に誤混入させない。

    months <= 0 は境界が今日になり、last_used が読める全サービスが休眠候補になってしまう。
    手順書は使用中のサービスまで退会対象にする。黙って候補を全量に広げず拒否する。
    """
    if months < 1:
        raise ValueError("months は 1 以上（0 やマイナスだと使用中のサービスまで退会候補になります）")
    year = today.year
    month = today.month - months
    while month <= 0:
        month += 12
        year -= 1
    day = min(today.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def weak_2fa(accounts: list[Account]) -> list[dict]:
    """2FA が無い／SMS だけのアカウント。重要カテゴリほど優先度を上げる。"""
    out = []
    for a in accounts:
        if a.two_factor in ("none", "sms", "email"):
            sev = "high" if a.category in HIGH_RISK_CATEGORIES else ("medium" if a.two_factor == "none" else "low")
            out.append({
                "service": a.service, "category": a.category, "two_factor": a.two_factor, "severity": sev,
                "suggestion": "パスキーかTOTPに切り替え" if a.two_factor != "none" else "2FAを有効化",
            })
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(out, key=lambda x: order[x["severity"]])


def dormant(accounts: list[Account], months: int = 12, today: date | None = None) -> list[dict]:
    """長期間使っていないアカウント（退会候補）。境界は暦月で判定し、利用日不明はここでは扱わない。"""
    today = today or date.today()
    cutoff = _months_ago(today, months)
    out = []
    for a in accounts:
        d = _parse(a.last_used)
        if d is None:
            continue  # 最終利用日が不明なものは audit.dormant では休眠扱いしない
        if d <= cutoff:
            days = (today - d).days
            out.append({"service": a.service, "last_used": a.last_used, "days_unused": days,
                        "has_payment": bool(a.payment_ref),
                        "suggestion": "退会 or 登録カード解除" if a.payment_ref else "退会を検討"})
    return sorted(out, key=lambda x: -x["days_unused"])


def change_impact(accounts: list[Account], field: str, old_value: str) -> list[dict]:
    """メール・電話・住所ラベル・カード愛称を変えたとき、更新が必要なサービス一覧。"""
    if field not in {"email", "phone", "address_label", "payment_ref"}:
        raise ValueError("field は email / phone / address_label / payment_ref のいずれか")
    hits = [a for a in accounts if getattr(a, field).lower() == old_value.lower()]
    # 重要なサービスから更新する
    hits.sort(key=lambda a: (a.category not in HIGH_RISK_CATEGORIES, a.service))
    return [{"service": a.service, "category": a.category, "current": getattr(a, field),
             "two_factor": a.two_factor,
             "note": "SMS 2FA のため電話番号を先に解除しないとログイン不能になる恐れ"
             if field == "phone" and a.two_factor == "sms" else ""} for a in hits]


def email_exposure(accounts: list[Account]) -> list[dict]:
    """同じメールアドレスに何サービスぶら下がっているか（漏えい時の影響範囲）。

    メールの大文字小文字は同一アドレス扱いで寄せる（Bitwarden の News@Example.com と
    Outlook の news@example.com が別グループになると、共有件数の警告が過小になる）。
    表示は最初に現れた表記を使う。
    """
    groups: dict[str, list[str]] = {}
    display: dict[str, str] = {}
    for a in accounts:
        if a.email:
            key = a.email.casefold()
            display.setdefault(key, a.email)
            groups.setdefault(key, []).append(a.service)
    return sorted(({"email": display[k], "count": len(s), "services": s} for k, s in groups.items()),
                  key=lambda x: -x["count"])


def summary(accounts: list[Account], today: date | None = None) -> dict:
    return {
        "total": len(accounts),
        "weak_2fa_high": sum(1 for x in weak_2fa(accounts) if x["severity"] == "high"),
        "dormant": len(dormant(accounts, today=today)),
        "emails": len(email_exposure(accounts)),
    }
