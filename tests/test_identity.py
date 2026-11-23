"""身份识别与历史隔离：多用户、多设备场景验收。"""
import os

import pytest

from core import history, identity


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """每个用例使用独立的存储根目录，互不影响。"""
    monkeypatch.setenv("CB_HISTORY_DIR", str(tmp_path))
    return tmp_path


def _blob(tag: str) -> bytes:
    return (f"%PDF-1.4 {tag}").encode() * 8


def test_register_login_logout_roundtrip():
    ok, res = identity.register("Alice", "secret1")
    assert ok and "成功" in res

    ok, token = identity.login("alice", "secret1")
    assert ok and token
    assert identity.user_from_token(token) == "alice"

    identity.logout(token)
    assert identity.user_from_token(token) is None


def test_register_rejects_duplicate_and_weak_password():
    assert identity.register("bob", "abcdef")[0]
    assert not identity.register("BOB", "abcdef")[0]      # 大小写归一后视为同一账号
    assert not identity.register("carol", "12345")[0]     # 密码过短


def test_login_rejects_wrong_password_and_unknown_user():
    identity.register("dave", "abcdef")
    assert not identity.login("dave", "wrong-pass")[0]
    assert not identity.login("nobody", "abcdef")[0]


def test_subject_differs_by_user_and_by_device():
    user_a = identity.subject("alice", "dev-1")
    user_b = identity.subject("bob", "dev-1")
    dev_1 = identity.subject(None, "dev-1")
    dev_2 = identity.subject(None, "dev-2")

    assert user_a != user_b          # 同设备不同账号 -> 不同主体
    assert dev_1 != dev_2            # 未登录不同设备 -> 不同主体
    assert dev_1 == identity.subject(None, "dev-1")   # 稳定可复现
    assert user_a != dev_1           # 登录态优先于设备标识


def test_history_isolated_between_two_users():
    a = identity.subject("alice", "shared-device")
    b = identity.subject("bob", "shared-device")
    rec = history.add_record(a, "alice.pdf", _blob("alice"), {"company": "A 公司"})

    assert [r["id"] for r in history.list_records(a)] == [rec["id"]]
    assert history.list_records(b) == []
    # 越权读取 / 删除一律失败
    assert history.get_record(b, rec["id"]) is None
    assert history.load_bytes(b, rec["id"]) is None
    assert history.delete_record(b, rec["id"]) is False
    assert history.get_record(a, rec["id"]) is not None


def test_history_isolated_between_two_devices():
    d1 = identity.subject(None, "device-1")
    d2 = identity.subject(None, "device-2")
    history.add_record(d1, "d1.pdf", _blob("d1"))

    assert len(history.list_records(d1)) == 1
    assert history.list_records(d2) == []
    assert history.clear_all(d2) == 0
    assert len(history.list_records(d1)) == 1


def test_clear_only_affects_own_subject():
    a = identity.subject("alice")
    b = identity.subject("bob")
    history.add_record(a, "a.pdf", _blob("a"))
    history.add_record(b, "b.pdf", _blob("b"))

    assert history.clear_all(a) == 1
    assert history.list_records(a) == []
    assert len(history.list_records(b)) == 1


def test_invalid_subject_rejected():
    for bad in ("", "../escape", "u_short", "x_" + "0" * 24, "d_" + "Z" * 24):
        assert history.list_records(bad) == []
        assert history.get_record(bad, "any") is None
        assert history.delete_record(bad, "any") is False
    with pytest.raises(ValueError):
        history.add_record("../escape", "x.pdf", _blob("x"))


def test_status_marks_missing_file():
    a = identity.subject("alice")
    rec = history.add_record(a, "a.pdf", _blob("a"))
    assert history.record_status(a, rec) == history.STATUS_OK
    os.remove(os.path.join(history._subject_dir(a), rec["file"]))
    assert history.record_status(a, rec) == history.STATUS_MISSING


def test_same_content_deduped_within_subject_only():
    a = identity.subject("alice")
    b = identity.subject("bob")
    r1 = history.add_record(a, "a.pdf", _blob("same"))
    r2 = history.add_record(a, "a.pdf", _blob("same"))
    r3 = history.add_record(b, "b.pdf", _blob("same"))

    assert r1["id"] == r2["id"]
    assert len(history.list_records(a)) == 1
    assert r3["id"] == r1["id"]      # 同哈希但各自独立存储
    assert len(history.list_records(b)) == 1


def test_total_size_and_legacy_migration(tmp_path):
    a = identity.subject(None, "legacy-device")
    legacy_index = tmp_path / "index.json"
    (tmp_path / "aaa.pdf").write_bytes(_blob("legacy"))
    legacy_index.write_text(
        '[{"id": "aaa", "name": "旧记录.pdf", "file": "aaa.pdf", "hash": "x", "size": 100}]',
        encoding="utf-8",
    )
    assert history.migrate_legacy(a) == 1
    records = history.list_records(a)
    assert records and records[0]["name"] == "旧记录.pdf"
    assert history.total_size(records) == 100
    assert not legacy_index.exists()          # 旧索引已移除，避免被重复认领
    assert history.migrate_legacy(identity.subject("bob")) == 0
