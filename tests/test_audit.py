import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from account_keeper import audit
from account_keeper.agent import offline_answer
from account_keeper.store import Account, Store

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "data" / "sample_accounts.json"
TODAY = date(2026, 10, 17)


@pytest.fixture
def store():
    s = Store(":memory:")
    s.import_json(SAMPLE)
    return s


def test_rejects_passwords():
    with pytest.raises(ValueError):
        Account.from_dict({"service": "x", "password": "hunter2"})


def test_weak_2fa_prioritizes_finance(store):
    rows = audit.weak_2fa(store.all())
    assert rows[0]["severity"] == "high"
    assert any(r["service"] == "MyBank Online" and r["severity"] == "high" for r in rows)
    assert all(r["two_factor"] in ("none", "sms", "email") for r in rows)


def test_dormant(store):
    names = [r["service"] for r in audit.dormant(store.all(), today=TODAY)]
    assert names[0] == "PhotoShare"
    assert "VideoSub" in names and "MicroBlog" not in names


def test_change_impact_phone_warns_sms(store):
    rows = audit.change_impact(store.all(), "phone", "090-0000-0001")
    assert rows[0]["category"] == "finance"
    assert any("SMS" in r["note"] for r in rows)


def test_offline_agent(store):
    assert "PhotoShare" in offline_answer(store, "放置してるアカウントある？")


def test_mcp_server_roundtrip(tmp_path):
    db = tmp_path / "a.db"
    Store(db).import_json(SAMPLE)
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "audit_2fa", "arguments": {}}},
    ]
    out = subprocess.run([sys.executable, "-m", "account_keeper.mcp_server"],
                         input="\n".join(json.dumps(m) for m in msgs), capture_output=True, text=True,
                         cwd=ROOT, env={"ACCOUNT_KEEPER_DB": str(db), "PATH": ""}, check=True).stdout
    resps = [json.loads(line) for line in out.splitlines()]
    assert [r["id"] for r in resps] == [1, 2, 3]
    assert "audit_2fa" in {t["name"] for t in resps[1]["result"]["tools"]}
    assert "MyBank Online" in resps[2]["result"]["content"][0]["text"]
