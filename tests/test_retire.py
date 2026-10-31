import json
import os
import sqlite3
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

import account_keeper.store as store_mod
from account_keeper import audit, retire
from account_keeper.agent import offline_answer
from account_keeper.store import Account, Store
from account_keeper.tools import build_tools

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "data" / "sample_accounts.json"
BW = ROOT / "data" / "sample_bitwarden.csv"
TODAY = date(2026, 10, 17)

@pytest.fixture
def store():
    s = Store(":memory:")
    s.import_json(SAMPLE)
    return s


def test_money_links_detection():
    assert "カード登録" in retire.money_links(Account(service="x", payment_ref="メインカード"))
    assert "金融" in retire.money_links(Account(service="x", category="finance"))
    assert "ゲーム" in retire.money_links(Account(service="GameGarden", url="gamegarden.example.net"))
    assert "家計簿" in retire.money_links(Account(service="BudgetNest", url="budgetnest.example.net"))
    assert retire.money_links(Account(service="PhotoShare", category="sns")) == []
    assert retire.priority(["カード登録"]) == 0
    assert retire.priority(["ゲーム"]) == 1
    assert retire.priority([]) == 2


def test_candidates_money_before_dormancy(store):
    cands = retire.candidates(store.all(), today=TODAY)
    names = [c["service"] for c in cands]
    assert names == ["FurimaPlus", "VideoSub", "PhotoShare", "TravelBooking"]
    assert names.index("VideoSub") < names.index("PhotoShare")  # 休眠日数よりお金優先
    assert cands[0]["rank"] == 1 and cands[0]["priority"] == 0
    assert "カード登録" in cands[0]["money_links"]


def test_candidates_exclude_recent(store):
    names = [c["service"] for c in retire.candidates(store.all(), today=TODAY)]
    assert "CodeHub" not in names and "MyBank Online" not in names


def test_candidates_email_note_with_primary_email(store):
    cands = retire.candidates(store.all(), today=TODAY, primary_emails=["taro.main@example.com"])
    by = {c["service"]: c for c in cands}
    assert "維持中のメール" in by["TravelBooking"]["email_note"]
    assert "維持中のメール外" in by["PhotoShare"]["email_note"]


def test_procedure_four_steps_in_owner_order(store):
    acc = store.get("FurimaPlus")
    assert acc is not None
    steps = retire.procedure(acc, ["FurimaPlus", "PhotoShare", "VideoSub"])
    assert [s["title"] for s in steps] == list(retire.STEP_TITLES)
    assert any("メインカード" in a for a in steps[0]["actions"])
    assert any("共有" in a for a in steps[3]["actions"])
    text = "\n".join(a for s in steps for a in s["actions"])
    assert "password" not in text.lower()


def test_procedure_warns_sms_phone_change(store):
    tb = store.get("TravelBooking")
    assert tb is not None
    steps = retire.procedure(tb, ["TravelBooking"])
    assert any("SMS" in a for a in steps[0]["actions"])


def test_render_manual_is_owner_run_and_has_no_secrets(store):
    cands = retire.candidates(store.all(), today=TODAY)
    text = retire.render_manual(cands, store.all())
    assert "実行は持ち主" in text and "エージェントはログインしない" in text
    for title in retire.STEP_TITLES:
        assert title in text
    assert all(c["service"] in text for c in cands)
    assert "password" not in text.lower()


def test_read_bitwarden_csv_ignores_password_column(tmp_path):
    p = tmp_path / "bw.csv"
    p.write_text(
        "name,uri,username,password,folder,notes\n"
        "GameGarden,gamegarden.example.net,taro@example.net,SENTINEL-NOT-STORED,Games,\n"
        "NoteNest,notenest.example.net,taro@example.net,SENTINEL-NOT-STORED,Notes,\n",
        encoding="utf-8")
    rows = retire.read_bitwarden_csv(p)
    assert [r["service"] for r in rows] == ["GameGarden", "NoteNest"]
    assert rows[0]["url"] == "gamegarden.example.net"
    text = "\n".join(f"{k}={v}" for r in rows for k, v in r.items())
    assert "SENTINEL-NOT-STORED" not in text


def test_import_bitwarden_csv_stores_url_only(tmp_path):
    s = Store(":memory:")
    count = retire.import_bitwarden_csv(s, BW)
    assert count >= 1
    acc = s.get("GameGarden")
    assert acc is not None
    assert acc.url == "gamegarden.example.net"
    assert not hasattr(acc, "password")
    assert "ゲーム" in retire.money_links(acc)


def test_retire_tools_flag_owner_execution(store):
    tools = build_tools(store)
    plan = tools["retire_plan"][1](12, ["taro.main@example.com"])
    assert plan[0]["service"] == "FurimaPlus"
    proc = tools["retire_procedure"][1]("FurimaPlus")
    assert proc["owner_runs"] is True and proc["agent_logs_in"] is False
    assert len(proc["steps"]) == 4
    with pytest.raises(ValueError):
        tools["retire_procedure"][1]("NoSuchService")


def test_offline_agent_returns_retire_procedure(store):
    out = offline_answer(store, "退会と連携解除の手順書を作って")
    assert "実行は持ち主" in out and "銀行側の連携解除" in out and "個人データ削除の確認" in out


def test_import_bitwarden_keeps_existing_fields(tmp_path):
    s = Store(":memory:")
    s.import_json(SAMPLE)
    retire.import_bitwarden_csv(s, BW)
    acc = s.get("FurimaPlus")
    assert acc is not None
    assert acc.url == "furima-plus.example.net"          # URL は補完
    assert acc.payment_ref == "メインカード"              # 既存のお金紐づきは保持
    assert acc.email == "taro.old@example.net"            # 既存のメールは保持
    assert "bitwarden" in acc.tags


def test_retire_cli_writes_manual(tmp_path):
    db = tmp_path / "a.db"
    out = tmp_path / "manual.md"
    Store(db).import_json(SAMPLE)
    subprocess.run([sys.executable, "-m", "account_keeper", "--db", str(db),
                    "retire", "--email", "taro.main@example.com", "--out", str(out)],
                   cwd=ROOT, check=True)
    text = out.read_text(encoding="utf-8")
    assert "実行は持ち主" in text and "エージェントはログインしない" in text
    assert retire.STEP_TITLES[0] in text and retire.STEP_TITLES[3] in text
    assert "password" not in text.lower()


# --- finding: 休眠境界は months*30 近似ではなく暦月で判定（固定1ケースだけだった境界を確かめる） ---
def test_candidates_boundary_is_calendar_month():
    today = date(2026, 1, 31)  # months*30 だと 360日前 = 2025-02-04 になって境界がズレる
    accts = [
        Account(service="OldJustUnderYear", last_used="2025-02-05"),  # 360日だが暦12ヶ月未満 → 除外
        Account(service="ExactlyYearAgo", last_used="2025-01-31"),    # ちょうど暦1年 → 候補
    ]
    cands = retire.candidates(accts, months=12, today=today)
    names = [c["service"] for c in cands]
    assert "OldJustUnderYear" not in names   # 1年未満が候補に混入してはいけない
    assert "ExactlyYearAgo" in names
    exact = {c["service"]: c for c in cands}["ExactlyYearAgo"]
    assert exact["status"] == "dormant" and exact["days_unused"] == 365


def test_audit_dormant_boundary_is_calendar_month():
    today = date(2026, 1, 31)
    rows = audit.dormant([Account(service="OldJustUnderYear", last_used="2025-02-05"),
                          Account(service="ExactlyYearAgo", last_used="2025-01-31"),
                          Account(service="NoDate", last_used="")], today=today)
    names = [r["service"] for r in rows]
    assert "OldJustUnderYear" not in names and "ExactlyYearAgo" in names
    assert "NoDate" not in names  # 利用日不明は audit.dormant で休眠扱いしない


# --- finding: last_used 空が无条件に退会候補の上位へ混入する（Bitwarden 取り込み直後の危険） ---
def test_unknown_last_used_not_top_retirement_candidate(tmp_path):
    s = Store(":memory:")
    retire.import_bitwarden_csv(s, BW)  # 取り込み直後は last_used が全部空
    cands = retire.candidates(s.all(), today=TODAY)
    assert cands and all(c["status"] == "unknown_last_used" for c in cands)
    text = retire.render_manual(cands, s.all())
    assert "要確認" in text and "退会候補にしない" in text
    assert retire.STEP_TITLES[0] not in text  # 利用日不明に退会手順（STEP1）を作らない


def test_unknown_ranked_after_dormant_despite_money_link():
    accts = [
        Account(service="FreshImported", category="finance", last_used="", payment_ref="カード"),  # priority0 だが不明
        Account(service="ReallyDormant", category="other", last_used="2023-01-01"),               # priority2 だが休眠確定
    ]
    cands = retire.candidates(accts, months=12, today=TODAY)
    assert [c["service"] for c in cands] == ["ReallyDormant", "FreshImported"]  # 不明をお金優先で上位にしない
    assert cands[0]["status"] == "dormant" and cands[1]["status"] == "unknown_last_used"


def test_offline_agent_separates_unknown_last_used(tmp_path):
    s = Store(":memory:")
    retire.import_bitwarden_csv(s, BW)
    out = offline_answer(s, "退会と連携解除の手順書を作って")
    assert "要確認" in out and "上から着手" not in out  # 全件不明では退会着手リストを作らない


# --- finding: Bitwarden 取り込みの複数レコード・重複名・URL 空の挙動（email保持は1件だけ検証） ---
def test_import_bitwarden_multiple_duplicate_and_empty_url(tmp_path):
    p = tmp_path / "bw.csv"
    p.write_text(
        "name,uri,username,password\n"
        "DupName,first.example.net,user,SENTINEL1\n"
        "DupName,,user,SENTINEL2\n"     # 重複名で URL 空 → 既存 URL を保持
        "NoUrl,,,SENTINEL3\n"           # 新規で URL 空 → url 空のまま作成
        ",x.example.net,user,SENTINEL4\n",  # name 空 → 読み飛ばす
        encoding="utf-8")
    s = Store(":memory:")
    count = retire.import_bitwarden_csv(s, p)
    assert count == 3
    dup = s.get("DupName")
    nou = s.get("NoUrl")
    assert dup is not None and nou is not None
    assert dup.url == "first.example.net"  # 空 URL で上書きしない
    assert nou.url == ""
    assert s.get("") is None               # name 空は作らない
    rows = retire.read_bitwarden_csv(p)
    assert all(set(r.keys()) <= {"service", "url", "tags", "notes"} for r in rows)  # username/password 列を持たない
    assert s.search("SENTINEL") == []  # password 列は Store に一切入らない


def test_import_bitwarden_empty_url_keeps_existing_fields(tmp_path):
    s = Store(":memory:")
    s.upsert(Account(service="FurimaPlus", category="shopping", email="taro.old@example.net",
                     payment_ref="メインカード", url="furima-plus.example.net", last_used="2024-03-02"))
    p = tmp_path / "bw.csv"
    p.write_text("name,uri,username,password\nFurimaPlus,,user,SENTINEL\n", encoding="utf-8")
    retire.import_bitwarden_csv(s, p)
    acc = s.get("FurimaPlus")
    assert acc is not None
    assert acc.url == "furima-plus.example.net"      # 空 URL で既存 URL を消さない
    assert acc.email == "taro.old@example.net"       # email 保持（複数件でも）
    assert acc.payment_ref == "メインカード"
    assert acc.last_used == "2024-03-02"             # last_used も保持
    assert s.search("SENTINEL") == []


# --- finding: 手順書が各サービスの実際の退会仕様と合っているかの検証（一般形の固定手順） ---
def test_procedure_flags_official_spec_check(store):
    acc = store.get("PhotoShare")
    text = "\n".join(a for s in retire.procedure(acc) for a in s["actions"])
    assert "公式ヘルプ" in text and "一般形" in text  # 固定手順ではなく公式仕様優先と明記
    assert acc.url in text or acc.service in text      # 対象サービスの URL/名前で確認させる
    manual = retire.render_manual(retire.candidates(store.all(), today=TODAY), store.all())
    assert "公式ヘルプ" in manual and "一般形" in manual


# --- finding: 秘密情報の扱い（CSV 自体・手順書の保存権限・漏えい） ---
def test_write_manual_uses_owner_only_permissions(tmp_path):
    p = retire.write_manual("sample body", tmp_path / "nested" / "manual.md")
    assert p.stat().st_mode & 0o777 == 0o600
    assert p.read_text(encoding="utf-8") == "sample body"


def test_retire_cli_manual_is_0600_and_secret_free(tmp_path):
    db = tmp_path / "a.db"
    out = tmp_path / "manual.md"
    Store(db).import_json(SAMPLE)
    subprocess.run([sys.executable, "-m", "account_keeper", "--db", str(db),
                    "retire", "--out", str(out)], cwd=ROOT, check=True)
    assert out.stat().st_mode & 0o777 == 0o600
    assert "password" not in out.read_text(encoding="utf-8").lower()


def test_import_bitwarden_cli_protects_db(tmp_path):
    db = tmp_path / "a.db"
    subprocess.run([sys.executable, "-m", "account_keeper", "--db", str(db),
                    "import-bitwarden", str(BW)], cwd=ROOT, check=True)
    assert db.stat().st_mode & 0o777 == 0o600  # 登録情報 DB を所有者のみ読み書きにする


# --- finding: months <= 0 だと境界が今日になり、使用中のサービスまで退会候補になる ---
def test_candidates_reject_non_positive_months(store):
    with pytest.raises(ValueError):
        retire.candidates(store.all(), months=0, today=TODAY)
    with pytest.raises(ValueError):
        retire.candidates(store.all(), months=-3, today=TODAY)
    with pytest.raises(ValueError):
        audit.dormant(store.all(), months=0, today=TODAY)


def test_retire_cli_rejects_non_positive_months(tmp_path):
    db = tmp_path / "a.db"
    out = tmp_path / "manual.md"
    Store(db).import_json(SAMPLE)
    r = subprocess.run([sys.executable, "-m", "account_keeper", "--db", str(db),
                        "retire", "--months", "0", "--out", str(out)],
                       cwd=ROOT, capture_output=True, text=True)
    assert r.returncode != 0
    assert not out.exists()  # 使用中のサービスを含む手順書を書き出していない


# --- finding: 金銭ヒントの部分一致（'pay' in display/repair）で無関係サービスが上席に来る ---
def test_money_hints_ignore_matches_inside_longer_words():
    unrelated = Account(service="DisplayCase", url="displaycase.example.net",
                        category="other", notes="repair and discount history")
    assert retire.money_links(unrelated) == []          # 決済扱いして上席にしない
    assert retire.money_links(Account(service="NotesApp", category="other",
                                      url="endgame-notes.example.net")) == []
    assert "決済" in retire.money_links(Account(service="PayNest", url="paynest.example.net"))
    assert "ゲーム" in retire.money_links(Account(service="MyGameZone", url="mygamezone.example.net"))
    assert "ゲーム" in retire.money_links(Account(service="Old App-Store", category="other"))  # 区切り含む語も検出


# --- finding: メール大文字小文字の違いで同一アドレスが複数グループになり共有件数が過小になる ---
def test_email_exposure_groups_case_insensitively():
    accts = [Account(service="NewsA", email="News@Example.com", last_used="2022-01-01"),
             Account(service="NewsB", email="news@example.com", last_used="2022-02-01")]
    rows = audit.email_exposure(accts)
    assert len(rows) == 1 and rows[0]["count"] == 2
    assert set(rows[0]["services"]) == {"NewsA", "NewsB"}
    cands = retire.candidates(accts, months=12, today=TODAY)
    assert all("2 件と共有" in c["email_note"] for c in cands)  # 過小警告にしない
    manual = retire.render_manual(cands, accts)
    assert "共有" in manual


# --- finding: 取り込み件数が「読み取り行数」と「Store のレコード数」で食い違う ---
def test_import_bitwarden_reports_rows_and_records(tmp_path):
    p = tmp_path / "bw.csv"
    p.write_text(
        "name,uri,username,password\n"
        "DupName,first.example.net,user,SENTINEL1\n"
        "DupName,second.example.net,user,SENTINEL2\n"
        "Solo,solo.example.net,user,SENTINEL3\n",
        encoding="utf-8")
    s = Store(":memory:")
    stats: dict = {}
    rows = retire.import_bitwarden_csv(s, p, stats=stats)
    assert rows == 3 and stats["rows_read"] == 3
    assert stats["created"] == 2 and stats["updated"] == 1 and stats["merged"] == 1
    assert len(s.all()) == 2  # 読み取り行数と Store 件数は別物として報告する
    dup = s.get("DupName")
    assert dup is not None and dup.url == "second.example.net"


# --- finding: 手順書のヘッダが実際に使った休眠境界（--months）でなく 12 ヶ月固定を書いてしまう ---
def test_manual_states_the_months_boundary_that_was_used():
    accts = [Account(service="Old9", last_used="2026-02-01", category="other"),
             Account(service="Recent5", last_used="2026-06-01", category="other")]
    cands6 = retire.candidates(accts, months=6, today=TODAY)
    assert [c["service"] for c in cands6] == ["Old9"]      # 6 ヶ月境界で選ばれた候補
    assert retire.candidates(accts, months=12, today=TODAY) == []  # 12 ヶ月では候補にならない

    manual6 = retire.render_manual(cands6, accts, months=6)
    assert "6 ヶ月" in manual6 and "12 ヶ月" not in manual6  # 実際の境界を明記
    assert "Old9" in manual6

    manual12 = retire.render_manual(retire.candidates(accts, months=12, today=TODAY), accts)
    assert "12 ヶ月" in manual12 and "6 ヶ月" not in manual12  # 既定は従来どおり 12 ヶ月


def test_retire_cli_manual_states_the_requested_months(tmp_path):
    db = tmp_path / "a.db"
    Store(db).import_json(SAMPLE)
    r = subprocess.run([sys.executable, "-m", "account_keeper", "--db", str(db),
                        "retire", "--months", "6"], cwd=ROOT, check=True,
                       capture_output=True, text=True)
    assert "6 ヶ月" in r.stdout and "既定 12 ヶ月" not in r.stdout


# --- finding: 既存レコード + CSV の URL 空での import-bitwarden（上書き境界の追加検証） ---
def test_import_bitwarden_empty_url_reimport_no_duplicate_tags(tmp_path):
    s = Store(":memory:")
    s.upsert(Account(service="FurimaPlus", category="shopping", email="taro.old@example.net",
                     payment_ref="メインカード", url="furima-plus.example.net",
                     last_used="2024-03-02", tags=["old"]))
    p = tmp_path / "bw.csv"
    p.write_text("name,uri,username,password\nfurimaplus,,user,SENTINEL\n", encoding="utf-8")  # 別表記・URL 空
    retire.import_bitwarden_csv(s, p)
    retire.import_bitwarden_csv(s, p)  # 2 回目（tag は既に付いている）
    accs = [a for a in s.all() if a.service.lower() == "furimaplus"]
    assert len(accs) == 1  # 表記違いでも重複レコードを作らない
    acc = accs[0]
    assert acc.service == "FurimaPlus"                 # 既存のサービス名（大文字表記）を維持
    assert acc.url == "furima-plus.example.net"        # 空 URL で消えない
    assert acc.email == "taro.old@example.net"
    assert acc.payment_ref == "メインカード"
    assert acc.last_used == "2024-03-02"
    assert acc.category == "shopping"
    assert acc.tags.count("bitwarden") == 1            # 複数回 import でも tag 重複追加なし
    assert "old" in acc.tags                           # 既存 tag も保持


def test_import_bitwarden_new_url_updates_only_url(tmp_path):
    s = Store(":memory:")
    s.upsert(Account(service="OldForum", category="other", email="taro.old@example.net",
                     last_used="2022-05-05", notes="何に登録したか怪しい"))
    p = tmp_path / "bw.csv"
    p.write_text("name,uri,username,password\nOldForum,oldforum.example.net,user,SENTINEL\n", encoding="utf-8")
    retire.import_bitwarden_csv(s, p)
    acc = s.get("OldForum")
    assert acc is not None
    assert acc.url == "oldforum.example.net"           # CSV 側の URL で補完
    assert acc.email == "taro.old@example.net"
    assert acc.last_used == "2022-05-05"
    assert acc.notes == "何に登録したか怪しい"          # 既存メモを Bitwarden 定型文で上書きしない
    assert s.search("SENTINEL") == []


# --- finding: months=12 の暦月境界（360〜365 日の境界・月末ズレ） ---
def test_candidates_boundary_sweep_360_to_365_days():
    today = date(2026, 10, 31)  # 暦 12 ヶ月前 = 2025-10-31（365 日前）。360〜364 日は 1 年未満
    accts = [Account(service=f"Unused{delta}d", last_used=str(today - timedelta(days=delta)))
             for delta in range(360, 367)]
    cands = retire.candidates(accts, months=12, today=today)
    names = {c["service"] for c in cands}
    assert names == {"Unused365d", "Unused366d"}  # 日数近似(360)でなく暦月で選ぶ
    exact = {c["service"]: c for c in cands}["Unused365d"]
    assert exact["status"] == "dormant" and exact["days_unused"] == 365


def test_candidates_boundary_across_leap_day():
    today = date(2028, 2, 29)  # うるう日。暦 12 ヶ月前は 2027-02-28（366 日前）
    accts = [
        Account(service="ExactlyTwelveMonths", last_used="2027-02-28"),  # 366 日だが暦ちょうど 12 ヶ月 → 候補
        Account(service="UnderTwelveMonths", last_used="2027-03-01"),    # 364 日 → 除外
    ]
    cands = retire.candidates(accts, months=12, today=today)
    assert [c["service"] for c in cands] == ["ExactlyTwelveMonths"]
    assert cands[0]["days_unused"] == 366


def test_months_ago_clamps_to_calendar_month_end():
    assert audit._months_ago(date(2026, 3, 31), 1) == date(2026, 2, 28)    # 31 日 → 存在しない 2/31 にしない
    assert audit._months_ago(date(2026, 5, 31), 1) == date(2026, 4, 30)    # 31 日 → 4/30 に揃える
    assert audit._months_ago(date(2028, 2, 29), 12) == date(2027, 2, 28)   # うるう日 → 前年は 2/28
    assert audit._months_ago(date(2026, 10, 31), 12) == date(2025, 10, 31) # 同月同日はそのまま


# --- finding: Bitwarden 取り込み直後（last_used 全空）と休眠候補が混在したときの手順書の分離 ---
def test_bitwarden_import_mixed_with_dormant_separates_unknown(tmp_path):
    s = Store(":memory:")
    s.import_json(SAMPLE)
    retire.import_bitwarden_csv(s, BW)  # GameGarden 等は新規 → last_used 空（実際に使用中の可能性が高い）
    cands = retire.candidates(s.all(), today=TODAY)
    by = {c["service"]: c for c in cands}
    assert by["FurimaPlus"]["status"] == "dormant"  # 既存の休眠確定はそのまま候補
    for name in ("GameGarden", "BudgetNest", "NoteNest", "OldForum"):
        assert by[name]["status"] == "unknown_last_used"
    assert min(c["rank"] for c in cands if c["status"] == "unknown_last_used") > \
        max(c["rank"] for c in cands if c["status"] == "dormant")  # 不明は休眠候補より後ろ
    for name in ("CodeHub", "MegaMart", "MyBank Online"):  # 実際に使用中（last_used 新しめ）は候補外
        assert name not in by
    text = retire.render_manual(cands, s.all())
    assert "要確認" in text
    for name in ("GameGarden", "BudgetNest", "NoteNest", "OldForum"):
        assert name in text
        assert f"## {by[name]['rank']}. {name}" not in text  # 利用日不明に 4 段退会手順を作らない
    assert f"## {by['FurimaPlus']['rank']}. FurimaPlus" in text  # 休眠候補には手順がある


# --- finding: 語頭一致でない語（'display' / 'repair' 型）の決済ヒント誤判定（別 hint 語でも確認） ---
def test_money_hints_no_false_positive_inside_words_for_other_hints():
    assert retire.money_links(Account(service="DiscardZone", url="discardzone.example.net")) == []   # 'card' in 'discard'
    assert retire.money_links(Account(service="MyRepay", url="myrepay.example.net",
                                      notes="repair shop display case")) == []                        # 'pay' in 'repay'/'repair'/'display'
    assert retire.money_links(Account(service="AccreditNow", url="accreditnow.example.org")) == []   # 'credit' in 'accredit'
    assert retire.priority(retire.money_links(Account(service="DiscardZone"))) == 2                   # 上席(0/1)にならない
    assert "決済" in retire.money_links(Account(service="RepairPayTools", url="repair.example.net/pay"))  # 語頭一致の本物は拾う


# --- finding: --months の 0 より小さい値（CLI とツール経路） ---
def test_retire_cli_rejects_negative_months(tmp_path):
    db = tmp_path / "a.db"
    out = tmp_path / "manual.md"
    Store(db).import_json(SAMPLE)
    r = subprocess.run([sys.executable, "-m", "account_keeper", "--db", str(db),
                        "retire", "--months=-1", "--out", str(out)],
                       cwd=ROOT, capture_output=True, text=True)
    assert r.returncode != 0
    assert "--months" in r.stderr  # 拒否理由が CLI エラーとして出る
    assert not out.exists()        # 使用中のサービスまで含む手順書は書かれない


def test_retire_tools_reject_non_positive_months(store):
    tools = build_tools(store)
    for bad in (0, -1, -12):
        with pytest.raises(ValueError):
            tools["retire_plan"][1](bad)
        with pytest.raises(ValueError):
            tools["find_dormant"][1](bad)


# --- finding: write_manual の既存ファイル上書き（完全置換・0600 正規化） ---
def test_write_manual_overwrite_replaces_content_and_normalizes_permissions(tmp_path):
    p = tmp_path / "manual.md"
    p.write_text("古い手順書 " * 200, encoding="utf-8")
    os.chmod(p, 0o644)  # 誰かの手で 0644 になっていたとする
    got = retire.write_manual("新しい本文", p)
    assert got == p
    assert p.read_text(encoding="utf-8") == "新しい本文"      # 完全置換（旧内容の残りが無い）
    assert p.stat().st_mode & 0o777 == 0o600                  # 上書きでも 0600 に戻す


# --- finding: url 追加前に作られた DB を開くと「no such column」等で Store 全体が使えなくなる（スキーマ移行） ---
def test_legacy_json_db_migrates_and_merges_case_variant_rows(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)  # url も service の NOCASE も無い以前の形
    conn.execute("CREATE TABLE accounts (service TEXT PRIMARY KEY NOT NULL, data TEXT NOT NULL)")
    conn.executemany("INSERT INTO accounts(service, data) VALUES(?, ?)", [
        ("FurimaPlus", json.dumps({"service": "FurimaPlus", "category": "shopping",
                                   "email": "taro.old@example.net", "payment_ref": "メインカード",
                                   "last_used": "2024-03-02", "tags": ["old"]}, ensure_ascii=False)),
        ("furimaplus", json.dumps({"service": "furimaplus", "notes": "うすい行"}, ensure_ascii=False)),
    ])
    conn.commit()
    conn.close()

    s = Store(db)                                   # 開くだけで失敗してはいけない
    assert len(s.all()) == 1                        # 表記違いの重複が 1 件に統合される
    acc = s.get("furimaplus")
    assert acc is not None
    assert acc.service == "FurimaPlus"              # 情報の多い側の表記を残す
    assert acc.email == "taro.old@example.net"      # うすい行で上書きされない
    assert acc.payment_ref == "メインカード"
    assert acc.url == ""                            # 無かったフィールドは既定値で補完
    s.upsert(Account(service="furimaplus", category="shopping", email="taro.old@example.net",
                     payment_ref="メインカード", url="furima-plus.example.net", last_used="2024-03-02"))
    got = s.get("FurimaPlus")
    assert got is not None and got.service == "FurimaPlus"              # 表記を維持して
    assert got.url == "furima-plus.example.net"                          # 移行後に url を追記できる

    reopened = Store(db)                                             # 再オープンで二度移行しない
    assert len(reopened.all()) == 1
    assert reopened.db.execute("PRAGMA user_version").fetchone()[0] == store_mod.SCHEMA_VERSION


def test_legacy_column_style_db_without_url_column_migrates(tmp_path):
    db = tmp_path / "cols.db"
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE accounts (
        service TEXT PRIMARY KEY NOT NULL, category TEXT, login_id TEXT, email TEXT, phone TEXT,
        two_factor TEXT, payment_ref TEXT, last_used TEXT, notes TEXT, tags TEXT)""")  # url 列の無い列持ち形
    conn.execute("INSERT INTO accounts VALUES(?,?,?,?,?,?,?,?,?,?)",
                 ("OldForum", "other", "", "taro.old@example.net", "", "none", "", "2022-05-05",
                  "何に登録したか怪しい", '["old"]'))
    conn.commit()
    conn.close()

    s = Store(db)                                   # 欠けた列を SELECT して壊れない
    acc = s.get("OldForum")
    assert acc is not None
    assert acc.email == "taro.old@example.net" and acc.last_used == "2022-05-05"
    assert acc.notes == "何に登録したか怪しい" and acc.tags == ["old"]
    assert acc.url == ""                            # 列の無いフィールドは既定値
    cands = retire.candidates([acc], months=12, today=TODAY)
    assert cands and cands[0]["service"] == "OldForum" and cands[0]["status"] == "dormant"
    s.upsert(Account(service="OldForum", category="other", email="taro.old@example.net",
                     url="oldforum.example.net", last_used="2022-05-05",
                     notes="何に登録したか怪しい", tags=["old"]))
    got = s.get("OldForum")
    assert got is not None and got.url == "oldforum.example.net"     # 新しいフィールドを保持できる


def test_migration_does_not_carry_forbidden_keys(tmp_path):
    db = tmp_path / "leaky.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE accounts (service TEXT PRIMARY KEY NOT NULL, data TEXT NOT NULL)")
    conn.execute("INSERT INTO accounts(service, data) VALUES(?, ?)",
                 ("LeakyService", json.dumps({"service": "LeakyService", "notes": "古いメモ",
                                              "password": "SENTINEL-PW"}, ensure_ascii=False)))
    conn.commit()
    conn.close()

    s = Store(db)                                   # 秘密情報混入だけで Store が開けなくならない
    acc = s.get("LeakyService")
    assert acc is not None and acc.notes == "古いメモ" and len(s.all()) == 1
    assert not hasattr(acc, "password")
    assert s.search("SENTINEL") == []
    assert "SENTINEL" not in s.db.execute("SELECT data FROM accounts").fetchone()[0]  # 移行先にも残さない


# --- finding: Bitwarden CSV の表記違い（FurimaPlus / furimaplus）で新規の重複レコードができる ---
def test_import_bitwarden_case_variant_updates_existing_instead_of_duplicating(tmp_path):
    s = Store(":memory:")
    s.upsert(Account(service="FurimaPlus", category="shopping", email="taro.old@example.net",
                     payment_ref="メインカード", last_used="2024-03-02", tags=["old"]))
    p = tmp_path / "bw.csv"
    p.write_text("name,uri,username,password\nfurimaplus,furima-plus.example.net,user,SENTINEL\n", encoding="utf-8")
    stats: dict = {}
    retire.import_bitwarden_csv(s, p, stats=stats)
    assert len(s.all()) == 1                        # 大文字小文字違いの重複を作らない
    acc = s.get("furimaplus")
    assert acc is not None and acc.service == "FurimaPlus"        # 既存の表記を維持
    assert acc.url == "furima-plus.example.net"     # 表記違いでも URL を補完
    assert acc.email == "taro.old@example.net" and acc.payment_ref == "メインカード"
    assert acc.tags == ["old", "bitwarden"]
    assert stats["created"] == 0 and stats["updated"] == 1 and stats["merged"] == 0
    assert s.search("SENTINEL") == []

    p2 = tmp_path / "bw2.csv"
    p2.write_text("name,uri,username,password\n"
                  "LedgerBox,ledgerbox.example.net,user,SENTINEL1\n"
                  "ledgerbox,ledgerbox.example.jp/old,user,SENTINEL2\n", encoding="utf-8")
    s2 = Store(":memory:")
    stats2: dict = {}
    assert retire.import_bitwarden_csv(s2, p2, stats=stats2) == 2
    assert len(s2.all()) == 1                       # CSV 内の表記違いも 1 レコードに寄せる
    assert stats2["created"] == 1 and stats2["updated"] == 1 and stats2["merged"] == 1
    merged = s2.get("LEDGERBOX")
    assert merged is not None and merged.service == "LedgerBox"
    assert merged.url == "ledgerbox.example.jp/old"  # 後の行の URL で補完される


# --- finding: import_bitwarden（MCP / エージェント経路）で登録情報 DB が 0600 にならない（CLI だけだった） ---
def test_import_bitwarden_tool_path_protects_db_permissions(tmp_path):
    db = tmp_path / "agent.db"
    tools = build_tools(Store(db))
    out = tools["import_bitwarden"][1](str(BW))
    assert out["imported"] >= 1 and out["rows_read"] == out["imported"]
    assert db.stat().st_mode & 0o777 == 0o600       # MCP 経由でも所有者のみ読み書きにする


# --- finding: write_manual が出力先のシンボリックリンクを追ってリンク先を上書きする ---
def test_write_manual_refuses_to_follow_symlink_output(tmp_path):
    secret = tmp_path / "important.txt"
    secret.write_text("持ち主の重要なファイル", encoding="utf-8")
    link = tmp_path / "manual.md"
    os.symlink(secret, link)
    with pytest.raises(ValueError) as exc:
        retire.write_manual("新しい手順書", link)
    assert "シンボリックリンク" in str(exc.value)
    assert secret.read_text(encoding="utf-8") == "持ち主の重要なファイル"  # 無関係なリンク先は破壊しない
    assert link.is_symlink()

    os.symlink(tmp_path / "nowhere.txt", tmp_path / "broken.md")  # 切れたリンクでも追わない
    with pytest.raises(ValueError):
        retire.write_manual("新しい手順書", tmp_path / "broken.md")
    assert not (tmp_path / "nowhere.txt").exists()


# --- finding: 現地化書き出し（Item Name 等）で項目名列が無く ValueError になる ---
def test_read_bitwarden_csv_accepts_localized_headers(tmp_path):
    p = tmp_path / "localized.csv"
    p.write_text(
        "Item Name,Folder,Username,Password,Homepage\n"
        "LedgerLite,Finance,SENTINEL-USER,SENTINEL-NOT-STORED,https://ledgerlite.example.net/home\n"
        "PlayYard,Games,taro@example.net,SENTINEL-NOT-STORED,playyard.example.net\n",
        encoding="utf-8")
    rows = retire.read_bitwarden_csv(p)
    assert [r["service"] for r in rows] == ["LedgerLite", "PlayYard"]  # "Item Name" を項目名列として使う
    assert rows[0]["url"] == "https://ledgerlite.example.net/home"     # username の name を誤用しない
    assert all(set(r.keys()) <= {"service", "url", "tags", "notes"} for r in rows)
    assert "folder:Finance" in rows[0]["tags"] and "folder:Games" in rows[1]["tags"]
    text = "\n".join(f"{k}={v}" for r in rows for k, v in r.items())
    assert "SENTINEL" not in text                    # username / password 列は読まない
    s = Store(":memory:")
    retire.import_bitwarden_csv(s, p)
    acc = s.get("LedgerLite")
    assert acc is not None and acc.url == "https://ledgerlite.example.net/home"
    assert acc.email == "" and acc.login_id == ""    # username 列の値を登録しない


def test_read_bitwarden_csv_without_name_column_gives_clear_error(tmp_path):
    p = tmp_path / "noname.csv"
    p.write_text("Username,Password,Notes,uri\nuser1,SENTINEL,note,x.example.net\n", encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        retire.read_bitwarden_csv(p)
    assert "項目名" in str(exc.value)                # username を項目名として使い捨てしない
    assert "SENTINEL" not in str(exc.value)          # 読んだ値をエラー文に流さない


# --- finding: uri / url 列が無い書き出しで URL が全部空になる（列名に無ければ値の形で特定） ---
def test_read_bitwarden_csv_detects_url_column_without_url_header(tmp_path):
    p = tmp_path / "website.csv"
    p.write_text("name,username,password,website\nPayNest,pay@example.net,SENTINEL,paynest.example.net/account\n",
                 encoding="utf-8")
    rows = retire.read_bitwarden_csv(p)
    assert rows[0]["url"] == "paynest.example.net/account"   # website 列を URL として使う
    p2 = tmp_path / "curlss.csv"
    p2.write_text("name,username,password,folder\nQuietTool,quiet@example.net,SENTINEL,Tools\n", encoding="utf-8")
    rows2 = retire.read_bitwarden_csv(p2)
    assert rows2[0]["url"] == ""                             # メール列を URL と誤認して補完しない
    assert retire.money_links(Account(service="QuietTool", tags=rows2[0]["tags"])) == []
    s = Store(":memory:")
    retire.import_bitwarden_csv(s, p)
    acc = s.get("PayNest")
    assert acc is not None and acc.url == "paynest.example.net/account"
    assert "決済" in retire.money_links(acc)


# --- finding: お金の手がかりが Bitwarden の folder（Finance 等）だけだと優先度が落ちる ---
def test_money_link_from_bitwarden_folder_only():
    links = retire.money_links(Account(service="LedgerLite", tags=["folder:Finance"]))
    assert "金融" in links and retire.priority(links) == 0   # folder だけでも最優先
    assert "ゲーム" in retire.money_links(Account(service="PlayYard", tags=["folder:Games"]))
    assert retire.money_links(Account(service="QuietTool", tags=["folder:Tools"])) == []
    assert retire.money_links(Account(service="FingerPaint", tags=["folder:FingerPaint"])) == []  # 誤発火しない
    accts = [Account(service="QuietTool", last_used="2024-01-02", tags=["folder:Tools"]),
             Account(service="LedgerLite", last_used="2024-01-01", tags=["folder:Finance"]),
             Account(service="PlayYard", last_used="2024-01-03", tags=["folder:Games"])]
    assert [c["service"] for c in retire.candidates(accts, months=12, today=TODAY)] == \
        ["LedgerLite", "PlayYard", "QuietTool"]              # folder 情報だけで上席に並ぶ


def test_import_bitwarden_keeps_folder_tag_for_money_priority(tmp_path):
    p = tmp_path / "bw.csv"
    p.write_text("name,uri,username,password,folder\nLedgerLite,ledgerlite.example.net,user,SENTINEL,Finance\n",
                 encoding="utf-8")
    s = Store(":memory:")
    retire.import_bitwarden_csv(s, p)
    acc = s.get("LedgerLite")
    assert acc is not None and "folder:Finance" in acc.tags
    assert "金融" in retire.money_links(acc)


# --- finding: retire --out が既存ファイルの配下など作れない場所のとき NotADirectoryError がそのまま出る ---
def test_retire_cli_out_that_cannot_be_created_is_a_clear_error(tmp_path):
    db = tmp_path / "a.db"
    Store(db).import_json(SAMPLE)
    blocker = tmp_path / "blocker"
    blocker.write_text("keep me", encoding="utf-8")
    for bad in (str(blocker / "manual.md"), str(blocker / "nested" / "manual.md")):
        r = subprocess.run([sys.executable, "-m", "account_keeper", "--db", str(db), "retire", "--out", bad],
                           cwd=ROOT, capture_output=True, text=True)
        assert r.returncode != 0
        assert "Traceback" not in r.stderr and "手順書を書き出せません" in r.stderr  # 理由がわかる CLI エラー
        assert not (tmp_path / "nested").exists()                                   # 半途半端を作らない
        assert blocker.read_text(encoding="utf-8") == "keep me"                     # 既存ファイルを壊さない


# --- finding: retire --out ~/retire-plan.md が展開されず文字通り "~" ディレクトリができる ---
def test_retire_cli_expands_tilde_in_out(tmp_path):
    db = tmp_path / "a.db"
    Store(db).import_json(SAMPLE)
    env = dict(os.environ, HOME=str(tmp_path))
    r = subprocess.run([sys.executable, "-m", "account_keeper", "--db", str(db), "retire", "--out", "~/retire-plan.md"],
                       cwd=ROOT, env=env, capture_output=True, text=True)
    assert r.returncode == 0
    plan = tmp_path / "retire-plan.md"
    assert plan.exists() and plan.stat().st_mode & 0o777 == 0o600   # HOME 配下に 0600 で出る
    assert "実行は持ち主" in plan.read_text(encoding="utf-8") and "password" not in plan.read_text(encoding="utf-8").lower()
    assert not (tmp_path / "~").exists()                            # 文字列 "~" のまま作らない
    assert "retire-plan.md" in r.stdout


# --- finding: primary_emails を渡すと共有アドレスの警告が消える（手順書側でも同じ） ---
def test_email_note_keeps_shared_warning_with_primary_emails(store):
    cands = retire.candidates(store.all(), today=TODAY, primary_emails=["taro.main@example.com"])
    by = {c["service"]: c for c in cands}
    travel = by["TravelBooking"]["email_note"]
    assert "維持中のメール" in travel and "7 件と共有" in travel    # 維持中でも共有件数を落とさない
    photo = by["PhotoShare"]["email_note"]
    assert "維持中のメール外" in photo and "3 件と共有" in photo
    manual = retire.render_manual(cands, store.all())
    assert "7 件と共有" in manual and "3 件と共有" in manual        # 手順書でも警告が消えない


# --- finding（ラウンド6）: 移行のケース違い統合が tags 本数だけで payment_ref / email より上になる ---
def test_migration_prefers_money_fields_over_tag_count(tmp_path):
    db = tmp_path / "weighted.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE accounts (service TEXT PRIMARY KEY NOT NULL, data TEXT NOT NULL)")
    conn.execute("INSERT INTO accounts(service, data) VALUES(?, ?)",
                 ("FurimaPlus", json.dumps({"service": "FurimaPlus", "tags": ["a", "b", "c", "d", "e"]},
                                           ensure_ascii=False)))
    conn.execute("INSERT INTO accounts(service, data) VALUES(?, ?)",
                 ("furimaplus", json.dumps({"service": "furimaplus", "payment_ref": "メインカード",
                                            "email": "taro.old@example.net", "last_used": "2024-03-02",
                                            "notes": "古めの通販"}, ensure_ascii=False)))
    conn.commit()
    conn.close()

    s = Store(db)
    assert len(s.all()) == 1
    acc = s.get("FurimaPlus")
    assert acc is not None
    assert acc.service == "furimaplus"                    # 完全度の高い（お金側）行の表記が残る
    assert acc.payment_ref == "メインカード"              # tags 本数で money 情報が負けない
    assert acc.email == "taro.old@example.net" and acc.last_used == "2024-03-02"
    assert set(acc.tags) == {"a", "b", "c", "d", "e"}     # tags は取りこぼさず和集合


# --- finding（ラウンド6）: 列持ち旧 DB の case 違い行が移行で統合され、空フィールドが埋まる ---
def test_legacy_column_style_db_merges_case_variant_rows_on_migration(tmp_path):
    db = tmp_path / "legacy-cols.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE accounts (service TEXT PRIMARY KEY NOT NULL, category TEXT, "
                 "email TEXT, last_used TEXT, notes TEXT, tags TEXT)")
    conn.execute("INSERT INTO accounts(service, category, email, last_used, notes, tags) VALUES(?,?,?,?,?,?)",
                 ("OldForum", "sns", "taro.old@example.net", "2023-01-01", "何に登録したか怪しい", '["old"]'))
    conn.execute("INSERT INTO accounts(service, category, email, last_used, notes, tags) VALUES(?,?,?,?,?,?)",
                 ("oldforum", None, None, None, "メモだけ", '["legacy", "tag2", "extra"]'))
    conn.commit()
    conn.close()

    s = Store(db)
    assert len(s.all()) == 1                               # 表記違いの 2 行が 1 件に寄る
    acc = s.get("OLDFORUM")
    assert acc is not None and acc.service == "OldForum"   # 完全度の高い行の表記を基準にする
    assert acc.email == "taro.old@example.net" and acc.last_used == "2023-01-01"  # 空だった側に埋める
    assert set(acc.tags) == {"old", "legacy", "tag2", "extra"}
    assert acc.two_factor == "none"                        # 無い情報を埋めの捏造はしない
    assert acc.url == ""


# --- finding（ラウンド6）: 移行だけ casefold() で寄せ、実行時 Store の COLLATE NOCASE と挙動が違う ---
def test_migration_only_merges_nocasable_case_variants(tmp_path):
    assert store_mod.nocase_key("Ångström") != store_mod.nocase_key("ångström")  # NOCASE は ASCII 限定

    db = tmp_path / "nocase.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE accounts (service TEXT PRIMARY KEY NOT NULL, data TEXT NOT NULL)")
    for service, payload in (
        ("Ångström", {"service": "Ångström", "email": "taro.a@example.net"}),
        ("ångström", {"service": "ångström", "url": "angstrom.example.net"}),
        ("LedgerBox", {"service": "LedgerBox", "email": "taro.b@example.net"}),
        ("ledgerbox", {"service": "ledgerbox", "url": "ledgerbox.example.net"}),
    ):
        conn.execute("INSERT INTO accounts(service, data) VALUES(?, ?)",
                     (service, json.dumps(payload, ensure_ascii=False)))
    conn.commit()
    conn.close()

    s = Store(db)
    assert len(s.all()) == 3                               # 非ASCII 表記違いは寄せない（実行時と同じ）
    assert s.get("Ångström") is not None and s.get("ångström") is not None  # 別行のまま
    nordic = s.get("ångström")
    assert nordic is not None and nordic.url == "angstrom.example.net" and nordic.email == ""
    ascii_pair = s.get("LedgerBox")
    assert ascii_pair is not None and ascii_pair.email == "taro.b@example.net"  # ASCII 違いは従来どおり統合
    assert ascii_pair.url == "ledgerbox.example.net"


# --- finding（ラウンド6）: 移行の途中失敗で accounts_pre_migration が残り accounts が空・途中状態になる ---
def test_migration_failure_leaves_old_db_untouched(tmp_path, monkeypatch):
    db = tmp_path / "rollback.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE accounts (service TEXT PRIMARY KEY NOT NULL, data TEXT NOT NULL)")
    for service in ("Aaaa", "Bbbb"):
        conn.execute("INSERT INTO accounts(service, data) VALUES(?, ?)",
                     (service, json.dumps({"service": service, "notes": "元データ"}, ensure_ascii=False)))
    conn.commit()
    conn.close()

    def boom(*args, **kwargs):
        raise RuntimeError("SENTINEL-BOOM")

    monkeypatch.setattr(store_mod.json, "dumps", boom)     # リネーム後の挿入段階で失敗させる
    with pytest.raises(RuntimeError):
        Store(db)

    conn = sqlite3.connect(db)                              # 失敗後に開き直すと、元 DB がそのまま残る
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"accounts"}                           # 半途半端なリネーム残置を作らない
    assert sorted(r[0] for r in conn.execute("SELECT service FROM accounts")) == ["Aaaa", "Bbbb"]
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0  # 未完移行を完了済みとして記録しない
    conn.close()

    monkeypatch.undo()
    s = Store(db)                                           # 次回の Store 起動で移行がやり直せる
    assert len(s.all()) == 2 and s.get("aaaa") is not None


# --- finding（ラウンド6）: '1.2' が URL 形に一致し、バージョン列が URL 列と誤認される ---
def test_read_bitwarden_csv_does_not_mistake_version_column_for_url(tmp_path):
    assert retire.URLISH_RE.match("1.2") is None           # 数値・バージョン値を URL と見なさない
    assert retire.URLISH_RE.match("paynest.example.net/account")
    p = tmp_path / "versioned.csv"
    p.write_text("name,username,password,version\n"
                 "QuietTool,quiet@example.net,SENTINEL,1.2\n"
                 "LedgerLite,ledger@example.net,SENTINEL,2024.1\n", encoding="utf-8")
    rows = retire.read_bitwarden_csv(p)
    assert [r["url"] for r in rows] == ["", ""]            # version 列を URL として取り込まない
    s = Store(":memory:")
    retire.import_bitwarden_csv(s, p)
    quiet = s.get("QuietTool")
    assert quiet is not None and quiet.url == ""


# --- finding（ラウンド6）: 壊れた後続 URL が前の正しい URL を静かに上書きする ---
def test_import_bitwarden_bad_url_does_not_overwrite_good_url(tmp_path):
    def import_csv(text: str) -> Store:
        p = tmp_path / "dup.csv"
        p.write_text(text, encoding="utf-8")
        s = Store(":memory:")
        stats: dict = {}
        retire.import_bitwarden_csv(s, p, stats=stats)
        return s

    good_then_bad = import_csv("name,uri,username,password\n"
                               "DupName,first.example.net,user,SENTINEL\n"
                               "DupName,not a url!!,user,SENTINEL\n")
    acc1 = good_then_bad.get("DupName")
    assert acc1 is not None and acc1.url == "first.example.net"      # 壊れた値で正しい URL を消さない

    bad_then_good = import_csv("name,uri,username,password\n"
                               "DupName,broken value,user,SENTINEL\n"
                               "DupName,good.example.net/exit,user,SENTINEL\n")
    acc2 = bad_then_good.get("DupName")
    assert acc2 is not None and acc2.url == "good.example.net/exit"  # まともな方へは更新される

    good_then_number = import_csv("name,uri,username,password\n"
                                  "DupName,first.example.net,user,SENTINEL\n"
                                  "DupName,1.2,user,SENTINEL\n")
    acc3 = good_then_number.get("DupName")
    assert acc3 is not None and acc3.url == "first.example.net"


# --- finding（ラウンド6）: 候補の service 表記が Account と違うと render_manual が KeyError で落ちる ---
def test_render_manual_tolerates_candidate_name_mismatch(store):
    cands = retire.candidates(store.all(), today=TODAY)
    for c in cands:
        c["service"] = c["service"].upper()                # 表記違い（大文字化）でも落ちない
    text = retire.render_manual(cands, store.all())
    assert all(f"## {c['rank']}. {c['service']}" in text for c in cands)
    assert "STEP 1" in text and "実行は持ち主" in text

    ghost = {"service": "GhostService", "category": "shopping", "url": "ghost.example.net",
             "email": "", "payment_ref": "", "days_unused": 500, "status": "dormant",
             "money_links": [], "priority": 2, "rank": 1,
             "email_note": "登録メール不明 → Bitwarden の項目と Outlook の登録メールを突き合わせる"}
    text2 = retire.render_manual([ghost], store.all())     # Store に無い候補でも手順を出す
    assert "## 1. GhostService" in text2 and "STEP 1" in text2
    assert "ghost.example.net" in text2


# --- finding（ラウンド6）: 登録メールが空のまま STEP4 が「共有中」の注意を出す（空アドレス共有に見える） ---
def test_procedure_shared_email_without_email_is_not_misleading():
    acc = Account(service="MysteryThing", category="other", email="")
    steps = retire.procedure(acc, ["MysteryThing", "OtherThing", "ThirdThing"])
    text4 = "\n".join(steps[3]["actions"])
    assert "件と共有" not in text4                          # 空メールを共有しているように見せない
    assert "突き合わせ" in text4                            # 共有先特定の手順へ置き換わる
    with_email = retire.procedure(Account(service="MysteryThing", email="taro.x@example.net"),
                                   ["MysteryThing", "OtherThing", "ThirdThing"])
    assert any("2 件と共有中" in a for a in with_email[3]["actions"])  # メールありの従来挙動は保つ


# --- finding（ラウンド6）: write_manual がハードリンク共有ファイルを書き換えてリンク元まで破壊する ---
def test_write_manual_refuses_to_overwrite_hardlink_target(tmp_path):
    secret = tmp_path / "important.txt"
    secret.write_text("持ち主の重要なファイル", encoding="utf-8")
    linked = tmp_path / "manual.md"
    os.link(secret, linked)                                 # 同じ inode を共有するハードリンク
    assert os.stat(str(linked)).st_nlink == 2
    with pytest.raises(ValueError) as exc:
        retire.write_manual("新しい手順書", linked)
    assert "ハードリンク" in str(exc.value)
    assert secret.read_text(encoding="utf-8") == "持ち主の重要なファイル"  # リンク元を壊さない
    assert linked.read_text(encoding="utf-8") == "持ち主の重要なファイル"  # 出力先も未変更


# --- finding（ラウンド6）: retire --out が親ディレクトリごと symlink 経由の場所だとリンク先Dirに書く ---
def test_retire_cli_refuses_out_under_symlinked_parent_dir(tmp_path):
    real = tmp_path / "realdir"
    real.mkdir()
    link = tmp_path / "linkdir"
    os.symlink(real, link)

    with pytest.raises(ValueError) as exc:
        retire.write_manual("新しい手順書", link / "manual.md")
    assert "シンボリックリンク" in str(exc.value)           # 親がリンクなら経由先へ書かない
    assert not (real / "manual.md").exists()

    db = tmp_path / "a.db"
    Store(db).import_json(SAMPLE)
    r = subprocess.run([sys.executable, "-m", "account_keeper", "--db", str(db), "retire",
                        "--out", str(link / "manual.md")],
                       cwd=ROOT, capture_output=True, text=True)
    assert r.returncode != 0
    assert "Traceback" not in r.stderr and "手順書を書き出せません" in r.stderr  # 理由がわかる CLI エラー
    assert "シンボリックリンク" in r.stderr
    assert not (real / "manual.md").exists()               # リンク先ディレクトリに書き出さない


# --- finding: サンプル CSV の行が見出しより 1 列多く、folder が notes にずれていた ---
def test_sample_bitwarden_rows_match_header():
    import csv
    with BW.open(encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    assert all(len(r) == len(rows[0]) for r in rows[1:])
    by_name = {r[0]: dict(zip(rows[0], r)) for r in rows[1:]}
    assert by_name["GameGarden"]["folder"] == "Games"
    assert by_name["BudgetNest"]["folder"] == "Finance"
