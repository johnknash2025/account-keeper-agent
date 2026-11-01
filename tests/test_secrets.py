import json
import sys
from dataclasses import asdict

import pytest

from account_keeper import retire
from account_keeper.store import FORBIDDEN_KEYS, Account, Store
from account_keeper.tools import build_tools

PASSWORD_SENTINEL = "SENTINEL-password-9f3k"
CARD_SENTINEL = "4111111111111111"


def _bitwarden_csv(tmp_path):
    path = tmp_path / "export.csv"
    path.write_text(
        "name,uri,username,password,folder,notes\n"
        f"PhotoShare,photoshare.example.net,taro.old@example.net,{PASSWORD_SENTINEL},SNS,放置中\n"
        f"FurimaPlus,furima-plus.example.net,taro.old@example.net,{PASSWORD_SENTINEL},Shopping,カード登録?\n"
        f"VideoSub,videosub.example.net,taro.main@example.com,,Entertainment,課金?\n",
        encoding="utf-8")
    return path


def _dump(store: Store) -> str:
    return json.dumps([asdict(a) for a in store.all()], ensure_ascii=False)


def test_upsert_account_tool_rejects_password(tmp_path):
    store = Store(tmp_path / "agent.db")
    tools = build_tools(store)
    with pytest.raises(ValueError):
        tools["upsert_account"][1](service="MegaMart", email="taro@example.com", password="hunter2")
    assert store.get("MegaMart") is None
    assert "hunter2" not in _dump(store)


def test_upsert_account_tool_rejects_card_number(tmp_path):
    store = Store(tmp_path / "agent.db")
    tools = build_tools(store)
    with pytest.raises(ValueError):
        tools["upsert_account"][1](service="MegaMart", payment_ref="メインカード", card_number=CARD_SENTINEL)
    assert store.get("MegaMart") is None
    assert CARD_SENTINEL not in _dump(store)


@pytest.mark.parametrize("secret_key", sorted(FORBIDDEN_KEYS))
def test_upsert_account_tool_rejects_every_forbidden_key(secret_key):
    store = Store(":memory:")
    tools = build_tools(store)
    with pytest.raises((ValueError, TypeError)):
        tools["upsert_account"][1](service="CloudConsole", **{secret_key: "x"})
    assert store.all() == []


def test_upsert_account_tool_rejects_secrets_on_update_without_partial_write(tmp_path):
    store = Store(tmp_path / "agent.db")
    tools = build_tools(store)
    upsert = tools["upsert_account"][1]
    saved = upsert(service="MyBank Online", category="finance", two_factor="totp", last_used="2026-10-10")
    with pytest.raises(ValueError):
        upsert(service="MyBank Online", password="hunter2")
    after = store.get("MyBank Online")
    assert asdict(after) == saved
    assert "hunter2" not in _dump(store)


def test_upsert_tool_schema_advertises_no_secret_fields():
    schema = build_tools(Store(":memory:"))["upsert_account"][0]["inputSchema"]
    advertised = {k.lower() for k in schema["properties"]}
    assert not (advertised & FORBIDDEN_KEYS), advertised & FORBIDDEN_KEYS
    assert schema["required"] == ["service"]


def test_import_bitwarden_tool_with_password_column_stores_nothing_secret(tmp_path):
    store = Store(tmp_path / "agent.db")
    tools = build_tools(store)
    out = tools["import_bitwarden"][1](str(_bitwarden_csv(tmp_path)))
    assert out["imported"] == 3 and out["rows_read"] == 3
    assert len(store.all()) == 3
    assert PASSWORD_SENTINEL not in _dump(store)
    for acc in store.all():
        record = asdict(acc)
        assert not (FORBIDDEN_KEYS & {k.lower() for k in record})
        assert all(PASSWORD_SENTINEL not in str(v) for v in record.values())
    assert store.get("FurimaPlus").url == "furima-plus.example.net"
    assert store.get("FurimaPlus").login_id == ""


def test_read_bitwarden_csv_never_returns_a_password_field(tmp_path):
    rows = retire.read_bitwarden_csv(_bitwarden_csv(tmp_path))
    assert rows
    for row in rows:
        assert not (FORBIDDEN_KEYS & {k.lower() for k in row})
        assert PASSWORD_SENTINEL not in json.dumps(row, ensure_ascii=False)


def test_imported_accounts_survive_reopen_without_secrets(tmp_path):
    db = tmp_path / "agent.db"
    build_tools(Store(db))["import_bitwarden"][1](str(_bitwarden_csv(tmp_path)))
    reopened = Store(db)
    assert len(reopened.all()) == 3
    assert PASSWORD_SENTINEL not in _dump(reopened)
    if sys.platform != "win32":
        assert db.stat().st_mode & 0o777 == 0o600


def test_account_from_dict_rejects_cased_variants():
    with pytest.raises(ValueError):
        Account.from_dict({"service": "x", "Password": "hunter2"})
    with pytest.raises(ValueError):
        Account.from_dict({"service": "x", "CARD_NUMBER": CARD_SENTINEL})
