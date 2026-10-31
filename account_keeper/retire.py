"""古いアカウントの洗い出しと、退会・連携解除の手順書（決定的ロジック）。

設計方針（担当: セキュリティ / issue 対応）:
- 材料は Bitwarden の項目名と URL（パスワードは持たない・読み込まない）と Outlook などの登録メール。
- お金とつながるサービス（家計簿・決済・ゲーム、カード登録済み）を上席に置く。
- 手順は 4 段固定: 銀行側の連携解除 → サービス内の解除 → 退会 → 個人データ削除の確認。
- エージェントはこの手順書を作るだけ。ログイン・退会の実際の操作は持ち主が行う。
"""
from __future__ import annotations

import csv
import os
import re
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Iterable

from . import audit
from .store import Account, Store, nocase_key

# 直接お金に接続するカテゴリ（銀行・証券 等）
DIRECT_CATEGORIES = {"finance"}

# サービス名・URL・メモに現れる語で「お金とのつながり」を補完する（家計簿・決済・ゲーム）
MONEY_HINTS: dict[str, tuple[str, ...]] = {
    "家計簿": ("家計簿", "家計", "household", "budget", "zaimoku", "kazumori", "money", "mint"),
    "決済": ("pay", "wallet", "payment", "billing", "checkout", "settlement", "qr", "bank",
             "card", "credit", "neobank"),
    "ゲーム": ("game", "gaming", "gamer", "gacha", "steam", "epic", "playstation", "psn",
               "nintendo", "switch", "xbox", "appstore", "app-store", "dmm"),
}

# 直接お金と見なす合図（これが最優先）
DIRECT_LINKS = {"金融", "カード登録"}

STEP_TITLES = (
    "銀行・カード側の連携解除",
    "サービス内の解除",
    "退会（アカウント削除申請）",
    "個人データ削除の確認",
)

OWNER_NOTE = "実行は持ち主。エージェントはログインしない。"

# Bitwarden 書き出しで読み込む列（これ以外は読まない。password 等は対象外）
NAME_KEYS = ("name", "item", "title", "項目名", "名前")
URL_KEYS = ("uri", "url")
URL_FALLBACK_KEYS = ("website", "web", "site", "domain", "link")
FOLDER_KEYS = ("folder", "collection", "group")
FOLDER_TAG_PREFIX = "folder:"
# 項目名列の代わりになり得ない列（username の "name" を拾わないため）
NON_NAME_TOKENS = {"username", "user", "login", "email", "password", "totp", "notes", "folder",
                   "type", "favorite", "reprompt", "expiry", "uri", "url"}
# 現地化などで列名が読めないとき、値の形だけで URL 列を特定する
# 最後のドメインラベルは英字開始（'1.2' のようなバージョン・数値列を URL と誤認しない）
URLISH_RE = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.\-]*://)?[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z][A-Za-z0-9\-]*(?::\d+)?(?:[/?#]\S*)?$")
# Bitwarden のフォルダ名だけでお金とわかる語（名前・URL にヒントが無くても優先対象にする）
# 短すぎる "fin" は FingerPaint 型で誤発火するので "finance" に留める
FINANCE_FOLDER_HINTS = ("finance", "bank", "money", "payment", "pay", "card", "credit",
                        "wallet", "billing", "家計", "金融", "銀行", "決済", "カード")


def _find_column(fields: dict[str, str], keys: tuple[str, ...],
                 reject_tokens: frozenset[str] | set[str] = frozenset(),
                 skip: tuple[str, ...] = ()) -> str | None:
    """lower 化ヘッダ辞書から対象列の元ヘッダ名を引く。現地化書き出し（Item Name 等）も拾う。

    1. 完全一致 2. 語トークン一致（Item Name → item / name） 3. 部分一致（ItemName）
    username の "name" のように別目的の列を誤用しないよう reject_tokens を効かせる。
    """
    for k in keys:
        if k in fields and fields[k] not in skip:
            return fields[k]
    keyset = set(keys)
    for header, original in fields.items():
        if original in skip:
            continue
        tokens = _ascii_tokens(header)
        if any(t in reject_tokens for t in tokens):
            continue
        if any(t in keyset for t in tokens):
            return original
    for header, original in fields.items():
        if original in skip or any(t in reject_tokens for t in _ascii_tokens(header)):
            continue
        if any(k in header for k in keys):
            return original
    return None


def _detect_url_column(fields: dict[str, str], raw_rows: list[dict], name_key: str) -> str | None:
    """列名に url / uri が無い（現地化書き出し等）とき、値の形から URL 列を特定する。

    メールアドレス（taro@example.net）は URL 形に一致させず、username 列を URL と誤認しない。
    """
    skip_tokens = (NON_NAME_TOKENS - {"uri", "url"}) | set(NAME_KEYS)
    best: tuple[int, str] | None = None
    for header, original in fields.items():
        if original == name_key or any(t in skip_tokens for t in _ascii_tokens(header)):
            continue
        filled = hits = 0
        for row in raw_rows:
            value = (row.get(original) or "").strip()
            if not value:
                continue
            filled += 1
            if URLISH_RE.match(value):
                hits += 1
        if filled and hits * 2 >= filled and (best is None or hits > best[0]):
            best = (hits, original)
    return best[1] if best else None


def folders_of(acc: Account) -> list[str]:
    """Bitwarden の folder / collection 列から作った folder:<名前> タグからフォルダ名を出す。"""
    out: list[str] = []
    for t in acc.tags:
        if t.startswith(FOLDER_TAG_PREFIX):
            name = t[len(FOLDER_TAG_PREFIX):].strip()
            if name and name not in out:
                out.append(name)
    return out


def _ascii_tokens(text: str) -> list[str]:
    """ASCII 語の語界トークン（区切り文字と camelCase で分割）。"""
    tokens: list[str] = []
    for part in re.split(r"[^0-9A-Za-z]+", text):
        tokens.extend(w.lower() for w in re.split(r"(?<=[a-z0-9])(?=[A-Z])", part) if w)
    return tokens


def _hint_hit(hint: str, hay: str) -> bool:
    """お金を示す語が「語の途中」で偶発的に一致して誤優先を作らないよう、
    ASCII 語は語頭一致で判定する（'pay' は display / repair には当たらず payment / PayNest に当たる）。
    区切りを含む語（app-store）と日本語語は部分一致のまま。
    """
    if hint.isascii() and hint.isalnum():
        return any(tok.startswith(hint) for tok in _ascii_tokens(hay))
    return hint in hay.lower()


def money_links(acc: Account) -> list[str]:
    """そのサービスがお金とつながっている理由（複数ありうる）。空なら非優先。

    Bitwarden の folder 列（Finance 等）だけにお金の手がかりがある場合も拾う。
    """
    links: list[str] = []
    if acc.category in DIRECT_CATEGORIES:
        links.append("金融")
    folders = folders_of(acc)
    if folders and "金融" not in links and any(_hint_hit(h, f) for f in folders for h in FINANCE_FOLDER_HINTS):
        links.append("金融")
    hay = " ".join([acc.service, acc.url, acc.category, acc.notes, *folders])
    for kind, hints in MONEY_HINTS.items():
        if kind not in links and any(_hint_hit(h, hay) for h in hints):
            links.append(kind)
    if acc.payment_ref:
        links.append("カード登録")
    return links


def priority(links: list[str]) -> int:
    """0 が最優先（銀行・カード登録）、1 はお金由来のヒント、2 はその他。"""
    if any(l in DIRECT_LINKS for l in links):
        return 0
    return 1 if links else 2


def candidates(accounts: list[Account], months: int = 12,
               today: date | None = None, primary_emails: Iterable[str] = ()) -> list[dict]:
    """休眠アカウントをお金優先で並べた退会候補。最近使った物と利用日不明は休眠候補に入れない。

    - 休眠境界は暦月（audit._months_ago）で判定。months*30 近似ではない。months < 1 は
      全サービスが休眠候補になる（使用中の物を退会させる）ので ValueError で拒否する。
    - last_used が空・読めないアカウントは status="unknown_last_used" として区別し、
      実際に使っているサービスを退会候補に誤混入させない（Bitwarden 取り込み直後は全部空）。
      これらは「要確認」扱いで休眠候補より後ろに置き、手順書でも休眠候補と同じ扱いにしない。
    """
    today = today or date.today()
    cutoff = audit._months_ago(today, months)
    primary = {e.lower() for e in primary_emails if e}
    exposure = {r["email"].casefold(): r["services"] for r in audit.email_exposure(accounts)}
    dormant_cands: list[dict] = []
    unknown_cands: list[dict] = []
    for a in accounts:
        d = audit._parse(a.last_used)
        if d is None:
            links = money_links(a)
            unknown_cands.append({
                "service": a.service, "category": a.category, "url": a.url, "email": a.email,
                "payment_ref": a.payment_ref, "days_unused": None, "status": "unknown_last_used",
                "money_links": links, "priority": priority(links),
                "email_note": _email_note(a, primary, exposure),
            })
            continue
        if d > cutoff:
            continue  # 最近使っている → 退会候補ではない
        days = (today - d).days
        links = money_links(a)
        dormant_cands.append({
            "service": a.service, "category": a.category, "url": a.url, "email": a.email,
            "payment_ref": a.payment_ref, "days_unused": days, "status": "dormant",
            "money_links": links, "priority": priority(links),
            "email_note": _email_note(a, primary, exposure),
        })
    dormant_cands.sort(key=lambda c: (c["priority"], -(c["days_unused"] or 0), c["service"]))
    unknown_cands.sort(key=lambda c: (c["priority"], c["service"]))
    out = dormant_cands + unknown_cands  # 休眠候補 → その後に利用日不明（要確認）
    for i, c in enumerate(out, 1):
        c["rank"] = i
    return out


def _email_note(a: Account, primary: set[str], exposure: dict[str, list[str]]) -> str:
    """維持中のメールかどうかと、そのアドレスを何件と共有しているかは別の話。両方書く。"""
    if not a.email:
        return "登録メール不明 → Bitwarden の項目と Outlook の登録メールを突き合わせる"
    shared = exposure.get(a.email.casefold(), [a.service])
    notes: list[str] = []
    if primary:
        if a.email.lower() in primary:
            notes.append(f"登録メール {a.email} は維持中のメール → 退会後に紐づきが残っていないか確認")
        else:
            notes.append(f"登録メール {a.email} は維持中のメール外 → 退会後このアドレス自体の後始末も検討")
    elif len(shared) <= 1:
        notes.append("登録メールはこの 1 件専用 → 退会後にアドレス自体も後始末")
    if len(shared) > 1:
        notes.append(f"登録メールは {len(shared)} 件と共有 → 全部退会するまでこのアドレスは止めない")
    return " ／ ".join(notes)


def service_specific_check(acc: Account) -> str:
    """4 段テンプレは一般形。実際の退会仕様は運営公式ヘルプで確認する、という必須確認事項。"""
    src = acc.url or acc.service
    return (
        f"{acc.service} の実際の退会・データ削除手順は運営の公式ヘルプで確認（{src}）。"
        "この手順書は一般形で、解約→削除の順序・削除後の猶予期間・データ書き出しの可否・"
        "有料/法人プランの規約はサービスごとに異なる。公式手順が常に優先。"
    )


def procedure(acc: Account, shared_email_services: list[str] | None = None) -> list[dict]:
    """1 サービス分の 4 段手順。持ち主が実行する前提で、エージェントはログインしない。"""
    steps: list[dict] = []

    if acc.payment_ref:
        s1 = [
            f"カード『{acc.payment_ref}』を発行した銀行・決済サービスの管理画面で {acc.service} への定期課金・自動引き落としを停止",
            f"{acc.service} に承認させたアプリ連携・OAuth 認可をカード / 口座側からも取り消し",
            "Card Guard Agent 側で、明細にこのサービスへの引き落としが消えたかを確認",
        ]
    else:
        s1 = [
            f"{acc.service} に紐づいた口座・カード・後払いがないか照会（あれば銀行側から先に停止）",
            f"{acc.service} が認可している外部アプリ・連携を連携元側から取り消し",
        ]
    if acc.two_factor == "sms":
        s1.append("SMS 2FA を使っている場合、電話番号の変更より先にこの解除を完了（後に変更するとサインイン不能になる恐れ）")
    steps.append({"step": 1, "title": STEP_TITLES[0], "actions": s1})

    s2 = [
        "設定 → 定期課金・サブスクリプションを解約（『退会』より解約が先にあるサービスに注意）",
        "サービス内に保存されたカード・口座・支払方法の登録を削除",
        "外部サインイン（Google / Apple / Facebook 等）との接続を解除",
        "アプリと端末から削除（残したいデータだけ先にダウンロード）",
    ]
    steps.append({"step": 2, "title": STEP_TITLES[1], "actions": s2})

    s3 = [
        service_specific_check(acc),
        "運営の退会・アカウント削除フォームから削除申請（ヘルプページの手順を先に確認）",
        "削除申請の控え（画面のスクリーンショット・確認メール）を保存",
        "削除完了までの猶予期間と、その間の再サインイン不可を確認",
    ]
    steps.append({"step": 3, "title": STEP_TITLES[2], "actions": s3})

    s4 = [
        "猶予期間経過後にサービスへ再接続し、削除されていることを確認",
        "確認メールや残存データが消えたかを確認。残っていれば運営へ削除・開示を請求",
    ]
    if shared_email_services is not None and len(shared_email_services) > 1:
        if acc.email:
            others = len([s for s in shared_email_services if s != acc.service])
            s4.append(f"登録メール {acc.email} は {others} 件と共有中。すべて退会するまでこのアドレスは止めない")
        else:
            # 登録メールが空のまま「メール共有」の注意を書くと、空のアドレスが共有されている
            # ように見える。共有先が特定できないなら突き合わせ手順に置き換える。
            s4.append("登録メール不明のまま他サービスと共有の恐れ。Bitwarden の項目と "
                      "Outlook の登録メールで共有アドレスの有無を突き合わせる")
    elif acc.email:
        s4.append(f"登録メール {acc.email} 宛の受信が消えたら完了。専用アドレスなら後始末も検討")
    steps.append({"step": 4, "title": STEP_TITLES[3], "actions": s4})
    return steps


def read_bitwarden_csv(path: Path | str) -> list[dict]:
    """Bitwarden の CSV 書き出し（持ち主がパスワードなしで出力）から項目名と URL だけ読む。

    列名は name / item / title 以外に "Item Name" のような現地化表記も拾い、
    uri / url 列が無い書き出しでは値の形から URL 列を特定する（無ければ空のまま）。
    folder 列は優先度判定に使うため folder:<名前> タグとして保持する（パスワード類は読まない）。
    """
    rows: list[dict] = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return rows
        fields = {h.lower().strip(): h for h in reader.fieldnames}
        name_key = _find_column(fields, NAME_KEYS, reject_tokens=NON_NAME_TOKENS)
        if name_key is None:
            raise ValueError(
                "Bitwarden CSV に name / item / title などの項目名列がありません。"
                "現地化された書き出しでは項目名列を含む形式で再出力してください")
        url_key = (_find_column(fields, URL_KEYS) or _find_column(fields, URL_FALLBACK_KEYS,
                    reject_tokens=NON_NAME_TOKENS, skip=(name_key,)))
        folder_key = _find_column(fields, FOLDER_KEYS)
        raw_rows = list(reader)
        if url_key is None:
            url_key = _detect_url_column(fields, raw_rows, name_key)
        for row in raw_rows:
            name = (row.get(name_key) or "").strip()
            if not name:
                continue
            url = (row.get(url_key) or "").strip() if url_key else ""
            tags = ["bitwarden"]
            folder = (row.get(folder_key) or "").strip() if folder_key else ""
            if folder:
                tags.append(f"{FOLDER_TAG_PREFIX}{folder}")
            rows.append({"service": name, "url": url, "tags": tags,
                         "notes": "Bitwarden の項目名と URL のみ（パスワードなし）"})
    return rows


def _pick_url(old: str, new: str) -> str:
    """同一サービスの 2 番目の URL をどう扱うか。新しい方がちゃんとした URL なら採用、
    壊れた値（'not a url!!' や '1.2'）が正しい URL を静かに上書きして失わせない。"""
    if not new.strip():
        return old
    if not old.strip():
        return new
    if URLISH_RE.match(new.strip()):
        return new
    if URLISH_RE.match(old.strip()):
        return old  # 壊れた後続値で正しい URL を消さない
    return new  # どちらも URL 形でないなら従来どおり後勝ち


def import_bitwarden_csv(store: Store, path: Path | str, stats: dict | None = None) -> int:
    """項目名と URL を Store に反映。既存レコードは email / last_used / payment_ref を残して URL だけ補完する。

    返り値は読み取り行数（同名の項目は 1 レコードにマージされるので Store の件数とは違う）。
    表記違い（FurimaPlus / furimaplus）も同一サービスとして扱い、既存レコードを更新する
    （大文字小文字違いの新規重複レコードを作らない）。
    stats を渡すと rows_read / created / updated / merged を入れて返す。
    """
    rows = read_bitwarden_csv(path)
    counts = {"rows_read": len(rows), "created": 0, "updated": 0, "merged": 0}
    seen: set[str] = set()
    for d in rows:
        existing = store.get(d["service"])
        if existing is None:
            store.upsert(Account.from_dict(d))
            counts["created"] += 1
        else:
            base = asdict(existing)
            base["url"] = _pick_url(base["url"], d["url"])
            for tag in d["tags"]:
                if tag not in base["tags"]:
                    base["tags"].append(tag)
            store.upsert(Account.from_dict(base))
            counts["updated"] += 1
        key = nocase_key(d["service"])  # Store の一意性（COLLATE NOCASE）と同じ基準で同名を数える
        if key in seen:
            counts["merged"] += 1
        seen.add(key)
    if stats is not None:
        stats.update(counts)
    return len(rows)


def render_manual(cands: list[dict], accounts: list[Account], months: int = 12) -> str:
    """洗い出し＋手順書を Markdown 1 本にまとめ、持ち主が実行する形で出力する。

    利用日不明（status=unknown_last_used）は退会候補の手順を作らず、
    「実際に使っていないか確認」の別枠に置いて、実際に使っているサービスを誤って退会させない。
    """
    exposure = {r["email"].casefold(): r["services"] for r in audit.email_exposure(accounts)}
    by_name: dict[str, Account] = {}
    for a in accounts:
        by_name.setdefault(nocase_key(a.service), a)  # 表記違い（FurimaPlus / furimaplus）でも引けるように
    dormant = [c for c in cands if c["status"] == "dormant"]
    unknown = [c for c in cands if c["status"] == "unknown_last_used"]
    lines = [
        "# 古いアカウント 洗い出しと退会・連携解除の手順書",
        "",
        f"- {OWNER_NOTE}（ログイン・退会操作は持ち主自身）",
        "- Bitwarden からは項目名と URL だけを使っています。パスワードはこの手順書にありません。",
        "- お金とつながるサービス（家計簿・決済・ゲーム・カード登録）を上席に置いてあります。",
        f"- 休眠境界は暦月（{months} ヶ月以上未使用）で判定。最終利用日が不明な物は退会候補にせず別枠で確認します。",
        "",
        "## 洗い出し（休眠候補・上から着手）",
        "",
    ]
    if not dormant:
        lines.append("（休眠候補なし）")
    for c in dormant:
        days = f"{c['days_unused']}日未使用"
        links = f" お金とつながり: {', '.join(c['money_links'])}" if c["money_links"] else " お金とのつながり: なし"
        lines.append(f"{c['rank']}. {c['service']}（{c['category']} / {days}）—{links}")
        lines.append(f"   - {c['email_note']}")
    if unknown:
        lines += ["", "## 要確認（最終利用日不明 / 退会候補にしない）", "",
                  "- Bitwarden 取り込み直後は最終利用日が空になります。実際に使っていれば退会しないでください。",
                  "- 利用状況を Outlook の登録メールや銀行明細で確認してから休眠候補へ昇格させてください。"]
        for c in unknown:
            links = f" お金とつながり: {', '.join(c['money_links'])}" if c["money_links"] else " お金とのつながり: なし"
            lines.append(f"・ {c['service']}（{c['category']} / 利用日不明）—{links}")
            lines.append(f"   - {c['email_note']}")
    for c in dormant:
        acc = by_name.get(nocase_key(c["service"]))
        if acc is None:
            # 候補と Account の表記が一致しなくても手順書生成をクラッシュさせない。
            # 候補側の情報から Account を組み立てて通常と同じ手順を出す。
            acc = Account(service=c["service"], category=c.get("category") or "other",
                          url=c.get("url") or "", email=c.get("email") or "",
                          payment_ref=c.get("payment_ref") or "")
        shared = exposure.get((acc.email or "").casefold(), [])
        lines += ["", f"## {c['rank']}. {c['service']}", ""]
        lines.append(f"- 対象 URL: {c['url'] or '（URL 登録なし — Bitwarden の項目名から探す）'}")
        lines.append(f"- {OWNER_NOTE}")
        for s in procedure(acc, shared):
            lines.append(f"\n### STEP {s['step']}: {s['title']}")
            for a in s["actions"]:
                lines.append(f"  - [ ] {a}")
    return "\n".join(lines) + "\n"


def write_manual(text: str, path: Path | str) -> Path:
    """手順書を 0600（所有者のみ読み書き）で保存。パス名に秘密情報は埋め込まない。

    - ~/... を展開する（展開しないと ~/.account-keeper/... がそのまま作られてしまう）
    - 出力先が既存ファイルの配下などディレクトリになり得ない場所なら、NotADirectoryError
      ではなく理由がわかる ValueError で拒否する
    - 出力先がシンボリックリンクのときは追わない（リンク先の上書き＝無関係なファイル破壊を避ける）
    - 出力先が親ディレクトリごとシンボリックリンク経由の場所なら、意図しないリンク先ディレクトリへ
      書かないよう拒否する
    - 出力先が別のファイルとハードリンク共有（inode 共有）なら書き換えない
      （上書きするとリンク元の重要ファイルまで同じ内容に書き換わる）
    """
    p = Path(str(path)).expanduser()
    parent = p.parent
    if str(parent) not in ("", "."):
        for anc in reversed(p.parents):  # 親のさらに親がリンクでも経由先はリンク先ディレクトリ
            if anc.is_symlink():
                raise ValueError(
                    f"手順書の出力先 {p} の親ディレクトリ {anc} はシンボリックリンクです"
                    f"（リンク先 {os.readlink(str(anc))} の中には書きません）")
        if parent.exists() and not parent.is_dir():
            raise ValueError(f"手順書の出力先 {p} を作れません（{parent} はディレクトリではありません）")
        parent.mkdir(parents=True, exist_ok=True)
    if p.is_symlink():
        raise ValueError(
            f"手順書の出力先 {p} はシンボリックリンクです（リンク先 {os.readlink(str(p))} は上書きしません）")
    if p.exists() and os.stat(str(p)).st_nlink > 1:
        raise ValueError(
            f"手順書の出力先 {p} は {os.stat(str(p)).st_nlink} 個のリンクが同じ inode を共有する"
            "ハードリンクです（上書きするとリンク元のファイルまで書き換わるため書きません）")
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(str(p), 0o600)
    return p
