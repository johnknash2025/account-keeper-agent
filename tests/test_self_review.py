"""レビュー成果物（.agent-tmp/self-review.json）の機械可読性を検査する。

前回: レビュアーの回答が JSON ではなく、findings を 1 件も読み出せなかった。
レビューが JSON として読めない状態で進めるのを、テストで失敗させる。
"""
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
REVIEW = ROOT / ".agent-tmp" / "self-review.json"

REQUIRED_KEYS = {"id", "kind", "severity", "title", "location", "detail", "fix", "status"}
SEVERITIES = {"high", "medium", "low"}
STATUSES = {"open", "fixed", "wontfix"}


def test_self_review_artifact_is_parseable_json():
    if not REVIEW.exists():
        pytest.skip("レビュー成果物是一次ファイル（.agent-tmp/）に存在しない")
    raw = REVIEW.read_text(encoding="utf-8")
    data = json.loads(raw)  # JSON でなければここで失敗する（前回の不具合）
    assert data["schema_version"] == 1
    assert isinstance(data["findings"], list) and data["findings"]


def test_self_review_findings_follow_the_schema():
    if not REVIEW.exists():
        pytest.skip("レビュー成果物是一次ファイル（.agent-tmp/）に存在しない")
    data = json.loads(REVIEW.read_text(encoding="utf-8"))
    ids = []
    for f in data["findings"]:
        assert REQUIRED_KEYS <= set(f), f"findings のキーが不足: {f}"
        assert all(isinstance(f[k], str) and f[k].strip() for k in REQUIRED_KEYS)
        assert f["severity"] in SEVERITIES
        assert f["status"] in STATUSES
        ids.append(f["id"])
    assert len(ids) == len(set(ids)), "findings id が重複"
    assert data["previous_reviewer_output_was_valid_json"] is False  # 前回は JSON として読めなかった
    assert data["reviewer_output_was_valid_json"] is True           # 今回は読み出せる形で再提出済み
