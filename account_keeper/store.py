"""ローカル SQLite ストア。

設計方針:
- パスワード・カード番号・秘密の質問の答えは「保存しない」。
  パスワードはパスワードマネージャー、カードは Card Guard Agent の担当。
- ここで持つのは「どのサービスに、どの連絡先・住所・2FA で登録したか」というメタ情報だけ。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

DEFAULT_DB = Path.home() / ".account-keeper" / "accounts.db"

# 保存を拒否するキー（誤って入れられないようにする）
FORBIDDEN_KEYS = {"password", "passcode", "pin", "card_number", "cvv", "secret_answer", "otp_seed"}

# スキーマバージョン（PRAGMA user_version）。マイグレーションを追加するたびに上げる。
SCHEMA_VERSION = 2
CANONICAL_TABLE = ("service", "data")
# service を NOCASE PRIMARY KEY にすることで、"FurimaPlus" と "furimaplus" の重複作成を防ぐ
CREATE_ACCOUNTS = """CREATE TABLE accounts (
    service TEXT PRIMARY KEY NOT NULL COLLATE NOCASE,
    data TEXT NOT NULL)"""


@dataclass
class Account:
    service: str
    category: str = "other"            # shopping / sns / finance / gov / work / game / other
    login_id: str = ""
    url: str = ""                      # Bitwarden 書き出しから取るサービス URL（認証情報は持たない）
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


def _table_columns(db: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {r["name"]: r for r in db.execute("PRAGMA table_info(accounts)")}


def _parse_tags(value) -> list[str]:
    """古い列持ちスキーマの tags 値（JSON 文字列 / カンマ区切り / 空）をリストに直す。"""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    text = str(value).strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return [t.strip() for t in text.split(",") if t.strip()]
    return [str(v) for v in parsed] if isinstance(parsed, list) else [text]


def _rows_as_dicts(db: sqlite3.Connection) -> list[dict]:
    """既存行を JSON blob 形・列持ち（古い）形のどちらからでも dict にする。

    列持ちスキーマに newer フィールド（url 等）の列が無い場合は、キー自体を出さない。
    Account.from_dict が既定値で補完するので、SELECT 側で「no such column」にはならない。
    """
    out: list[dict] = []
    for row in db.execute("SELECT * FROM accounts"):
        keys = row.keys()
        if "data" in keys and len(keys) == 2:
            out.append(json.loads(row["data"]))
            continue
        d: dict = {}
        for k in keys:
            if k == "tags":
                d[k] = _parse_tags(row[k])
            elif k in Account.__dataclass_fields__ and row[k] is not None:
                d[k] = row[k]
        out.append(d)
    return out


# ケース違いマージで「情報量」を測る重み。tags の本数だけで
# payment_ref / email（お金と連絡の要）を上回らないようにする。
KEY_WEIGHTS = {"payment_ref": 5, "email": 4, "url": 4, "last_used": 4, "category": 2,
               "login_id": 2, "phone": 2, "address_label": 2, "notes": 1}
LIST_WEIGHT_CAP = 2  # tags 等のリストは 1 項目 1 点ではなく上限付き


def _completeness(d: dict) -> int:
    """ケース違いの行をマージするとき、情報の多い方（email / url / payment_ref 等）を残すための評価。"""
    score = 0
    for k, v in d.items():
        if k == "service":
            continue
        if isinstance(v, list):
            filled = len([t for t in v if str(t).strip()])
            score += min(filled, LIST_WEIGHT_CAP) if filled else 0
        elif str(v).strip():
            if k == "two_factor":
                score += 0 if str(v).strip().lower() == "none" else 1
            else:
                score += KEY_WEIGHTS.get(k, 1)
    return score


def _merge_variant(base: dict, other: dict) -> dict:
    """ケース違いの 2 行を 1 件に寄せる。完全度の高い方を基準に、
    空のフィールドをもう一方から埋め、tags（folder 等）は取りこぼさないよう和集合にする。"""
    merged = dict(base)
    for k, v in other.items():
        if k == "service":
            continue
        cur = merged.get(k)
        if isinstance(cur, list) and isinstance(v, list):
            merged[k] = cur + [t for t in v if t not in cur]
        elif isinstance(cur, list) or isinstance(v, list):
            continue  # スカラーとリストは混ぜない（tags の形が壊れる）
        elif (not str(cur).strip() or (k == "two_factor" and str(cur).strip().lower() == "none")) \
                and str(v).strip() and not (k == "two_factor" and str(v).strip().lower() == "none"):
            merged[k] = v
    return merged


def nocase_key(s: str) -> str:
    """SQLite COLLATE NOCASE と同じ ASCII 限定の大文字小文字正規化。
    casefold()（ß→ss や Å→å まで寄せる）と違うので、実行時の Store の挙動と一致させる。"""
    return "".join(chr(ord(c) + 32) if "A" <= c <= "Z" else c for c in s)


def _service_is_nocase(db: sqlite3.Connection) -> bool:
    """PRAGMA table_info は collation を返さないので、accounts の作成 SQL から service の
    COLLATE NOCASE を検査する（NOCASE でない DB は一意性が case sensitive のため再造作が必要）。"""
    row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='accounts'").fetchone()
    return bool(row and row["sql"] and re.search(r"\bservice\b[^,]*\bnocase\b", row["sql"], re.I))


def _schema_is_current(db: sqlite3.Connection) -> bool:
    cols = _table_columns(db)
    if not cols:
        return False
    if tuple(cols) != CANONICAL_TABLE:
        return False
    if not _service_is_nocase(db):
        return False
    return db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def _migrate(db: sqlite3.Connection) -> None:
    """古い DB（url 列が無い / service が BINARY のまま）を現在のスキーマへ移行する。

    - 列持ちスキーマ → (service, data) JSON blob 形へ変換し、欠けたフィールドは既定値で補完
    - service の collation を NOCASE にし、大文字小文字違いの重複行を 1 件へ統合
      （統合キーは実行時 Store と同じ ASCII NOCASE。Ångström / ångström のような
      NOCASE が区別する表記はそのまま別行に残す）
    - PRAGMA user_version に現在のスキーマバージョンを記録
    - 移行の途中で失敗したら ROLLBACK（accounts_pre_migration だけ残して accounts が
      空・途中状態になると、以降の Store 操作が全部壊れる）
    """
    merged: dict[str, dict] = {}
    for row in _rows_as_dicts(db):
        # 古い DB に誤って混入した秘密情報は移行先に引き継がない。
        # だからといって行ごと捨てると Store 全体が開けなくなるので、危険なキーだけ落として移行する。
        d = {k: v for k, v in row.items() if k.lower() not in FORBIDDEN_KEYS}
        acc = Account.from_dict(d)
        if not acc.service:
            continue
        record = asdict(acc)
        key = nocase_key(acc.service)
        if key not in merged:
            merged[key] = record
        elif _completeness(record) > _completeness(merged[key]):
            merged[key] = _merge_variant(record, merged[key])
        else:
            merged[key] = _merge_variant(merged[key], record)
    old_isolation = db.isolation_level
    db.isolation_level = None  # BEGIN / COMMIT / ROLLBACK を明示制御して一括移行にする
    try:
        db.execute("BEGIN")
        db.execute("DROP TABLE IF EXISTS accounts_pre_migration")  # 移行途中で落ちた後の再実行を可能にする
        db.execute("ALTER TABLE accounts RENAME TO accounts_pre_migration")
        db.execute(CREATE_ACCOUNTS)
        db.executemany("INSERT INTO accounts(service, data) VALUES(?, ?)",
                      [(d["service"], json.dumps(d, ensure_ascii=False)) for d in merged.values()])
        db.execute("DROP TABLE accounts_pre_migration")
        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        db.execute("COMMIT")
    except Exception:
        try:
            db.execute("ROLLBACK")  # リネーム途中・挿入失敗でも元の accounts をそのまま復旧させる
        except sqlite3.Error:
            pass  # ロールバックすべき生きたトランザクションが無い失敗（BEGIN 前）では元エラーを出す
        raise
    finally:
        db.isolation_level = old_isolation


class Store:
    def __init__(self, path: Path | str = DEFAULT_DB):
        self.path = Path(str(path)).expanduser()
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        if self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='accounts'").fetchone() is None:
            self.db.execute(CREATE_ACCOUNTS)
            self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self.db.commit()
        elif not _schema_is_current(self.db):
            _migrate(self.db)

    def upsert(self, acc: Account) -> Account:
        existing = self.get(acc.service)
        record = asdict(acc)
        # 表記違い（FurimaPlus / furimaplus）で列と JSON の service がズレないよう、既存の表記を維持する
        record["service"] = existing.service if existing is not None else acc.service
        self.db.execute(
            "INSERT INTO accounts(service, data) VALUES(?, ?) "
            "ON CONFLICT(service) DO UPDATE SET data=excluded.data",
            (record["service"], json.dumps(record, ensure_ascii=False)),
        )
        self.db.commit()
        return Account.from_dict(record)

    def secure(self) -> None:
        """登録情報 DB を所有者のみ読み書き（0600）にする。メモリ DB では何もしない。"""
        if str(self.path) != ":memory:" and self.path.exists():
            os.chmod(self.path, 0o600)

    def _account(self, row: sqlite3.Row) -> Account:
        data = json.loads(row["data"])
        data["service"] = row["service"]  # 列側の表記を正とし、JSON 表記とズレさせない
        return Account.from_dict(data)

    def get(self, service: str) -> Account | None:
        row = self.db.execute("SELECT service, data FROM accounts WHERE service = ? COLLATE NOCASE",
                              (service,)).fetchone()
        return self._account(row) if row else None

    def all(self) -> list[Account]:
        rows = self.db.execute("SELECT service, data FROM accounts ORDER BY service COLLATE NOCASE").fetchall()
        return [self._account(r) for r in rows]

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
