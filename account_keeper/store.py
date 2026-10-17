"""ローカル SQLite ストア。

設計方針:
- パスワード・カード番号・秘密の質問の答えは「保存しない」。
  パスワードはパスワードマネージャー、カードは Card Guard Agent の担当。
- ここで持つのは「どのサービスに、どの連絡先・住所・2FA で登録したか」というメタ情報だけ。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

DEFAULT_DB = Path.home() / ".account-keeper" / "accounts.db"

# 保存を拒否するキー（誤って入れられないようにする）
FORBIDDEN_KEYS = {"password", "passcode", "pin", "card_number", "cvv", "secret_answer", "otp_seed"}


@dataclass
class Account:
    service: str
    category: str = "other"            # shopping / sns / finance / gov / work / other
    login_id: str = ""
    email: str = ""
    phone: str = ""
    address_label: str = ""            # 住所そのものではなく「自宅」「旧住所」などのラベル
    two_factor: str = "none"           # none / sms / totp / passkey / email
    payment_ref: str = ""              # Card Guard 側のカード「愛称」だけを参照（番号は持たない）
    last_used: str = ""                # YYYY-MM-DD
    notes: str = ""
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "Account":
        bad = FORBIDDEN_KEYS & {k.lower() for k in d}
        if bad:
            raise ValueError(f"保存できない項目が含まれています: {sorted(bad)}（パスワード等は扱いません）")
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


class Store:
    def __init__(self, path: Path | str = DEFAULT_DB):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS accounts (
                service TEXT PRIMARY KEY, data TEXT NOT NULL)"""
        )

    def upsert(self, acc: Account) -> Account:
        self.db.execute(
            "INSERT INTO accounts(service, data) VALUES(?, ?) "
            "ON CONFLICT(service) DO UPDATE SET data=excluded.data",
            (acc.service, json.dumps(asdict(acc), ensure_ascii=False)),
        )
        self.db.commit()
        return acc

    def get(self, service: str) -> Account | None:
        row = self.db.execute("SELECT data FROM accounts WHERE service = ? COLLATE NOCASE", (service,)).fetchone()
        return Account.from_dict(json.loads(row["data"])) if row else None

    def all(self) -> list[Account]:
        rows = self.db.execute("SELECT data FROM accounts ORDER BY service").fetchall()
        return [Account.from_dict(json.loads(r["data"])) for r in rows]

    def delete(self, service: str) -> bool:
        cur = self.db.execute("DELETE FROM accounts WHERE service = ? COLLATE NOCASE", (service,))
        self.db.commit()
        return cur.rowcount > 0

    def search(self, query: str) -> list[Account]:
        q = query.lower()
        return [a for a in self.all() if q in json.dumps(asdict(a), ensure_ascii=False).lower()]

    def import_json(self, path: Path | str) -> int:
        items = json.loads(Path(path).read_text(encoding="utf-8"))
        for d in items:
            self.upsert(Account.from_dict(d))
        return len(items)


def today() -> date:
    return date.today()
