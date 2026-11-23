"""上传历史：按身份主体隔离持久化财报记录。

存储布局（根目录默认 data/uploads，可用环境变量 CB_HISTORY_DIR 覆盖）：
  accounts.json / sessions.json          账号与登录态（见 core.identity）
  subjects/<subject>/index.json          该主体的记录列表（按上传时间倒序）
  subjects/<subject>/<id>.pdf            原始文件，删除记录时同步删除

subject 由 core.identity 依据「登录账号」或「设备标识」计算得出且不可逆，
因此不同用户 / 不同设备写入不同目录，读取与删除也只在本主体目录内进行，
从存储层杜绝跨主体访问。同一文件按内容哈希去重，不改变首次上传时间。
"""
import hashlib
import json
import os
from datetime import datetime

from .identity import base_dir, is_valid_subject

MAX_RECORDS = 12
STATUS_OK = "已解析"
STATUS_MISSING = "文件缺失"


def _subject_dir(subject: str) -> str:
    return os.path.join(base_dir(), "subjects", subject)


def _index_path(subject: str) -> str:
    return os.path.join(_subject_dir(subject), "index.json")


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _read_index(subject: str) -> list:
    if not is_valid_subject(subject):
        return []
    try:
        with open(_index_path(subject), encoding="utf-8") as fh:
            records = json.load(fh)
    except (OSError, ValueError):
        return []
    if not isinstance(records, list):
        return []
    return [r for r in records if isinstance(r, dict) and r.get("id")]


def _write_index(subject: str, records: list) -> None:
    """原子写入，避免进程中断留下半个 JSON 文件。"""
    path = _index_path(subject)
    _ensure_dir(os.path.dirname(path))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(records, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _remove_file(subject: str, record: dict) -> None:
    if not record.get("file"):
        return
    try:
        path = os.path.join(_subject_dir(subject), record["file"])
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def list_records(subject: str, limit: int = MAX_RECORDS) -> list:
    """按上传时间倒序返回该主体的记录，最多 limit 条。"""
    return _read_index(subject)[:limit]


def get_record(subject: str, rid: str):
    """仅在本主体索引内查找，其他主体的记录 id 一律查不到。"""
    return next((r for r in _read_index(subject) if r.get("id") == rid), None)


def load_bytes(subject: str, rid: str):
    """读取本主体某条记录的原始 PDF 内容；文件缺失或不属于本主体时返回 None。"""
    record = get_record(subject, rid)
    if not record or not record.get("file"):
        return None
    try:
        with open(os.path.join(_subject_dir(subject), record["file"]), "rb") as fh:
            return fh.read()
    except OSError:
        return None


def record_status(subject: str, record: dict) -> str:
    """记录状态：文件存在为已解析，文件丢失则标记文件缺失。"""
    if not record.get("file"):
        return STATUS_MISSING
    path = os.path.join(_subject_dir(subject), record["file"])
    if not os.path.exists(path):
        return STATUS_MISSING
    return record.get("status") or STATUS_OK


def total_size(records: list) -> int:
    return sum(int(r.get("size") or 0) for r in records)


def add_record(subject: str, name: str, blob: bytes, meta: dict = None) -> dict:
    """保存一次上传到当前主体；同内容文件只保留首次记录。"""
    if not is_valid_subject(subject):
        raise ValueError("无效的主体标识。")
    digest = hashlib.sha1(blob).hexdigest()
    records = _read_index(subject)
    for record in records:
        if record.get("hash") == digest:
            return record

    rid = digest[:12]
    fname = f"{rid}.pdf"
    _ensure_dir(_subject_dir(subject))
    with open(os.path.join(_subject_dir(subject), fname), "wb") as fh:
        fh.write(blob)

    meta = meta or {}
    record = {
        "id": rid,
        "name": os.path.basename(name or "未命名.pdf"),
        "file": fname,
        "hash": digest,
        "size": len(blob),
        "company": meta.get("company") or "",
        "period": f"{meta.get('year') or ''}{meta.get('period') or ''}".strip(),
        "uploaded_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "status": STATUS_OK,
    }
    records.insert(0, record)
    for old in records[MAX_RECORDS:]:
        _remove_file(subject, old)
    _write_index(subject, records[:MAX_RECORDS])
    return record


def delete_record(subject: str, rid: str) -> bool:
    """删除本主体的记录并同步删除 PDF 文件；不属于该主体时返回 False。"""
    records = _read_index(subject)
    target = next((r for r in records if r.get("id") == rid), None)
    if target is None:
        return False
    _remove_file(subject, target)
    _write_index(subject, [r for r in records if r.get("id") != rid])
    return True


def clear_all(subject: str) -> int:
    """清空本主体的全部记录并删除 PDF 文件，返回清除条数。"""
    records = _read_index(subject)
    for record in records:
        _remove_file(subject, record)
    _write_index(subject, [])
    return len(records)


def migrate_legacy(subject: str) -> int:
    """把旧版「无主体」记录一次性迁入当前主体。

    仅当该主体尚无数据、且旧版 index.json 存在时执行；迁移后删除旧索引，
    避免旧数据被其他主体重复认领。返回迁移条数。
    """
    if not is_valid_subject(subject):
        return 0
    legacy_index = os.path.join(base_dir(), "index.json")
    if not os.path.exists(legacy_index) or os.path.exists(_index_path(subject)):
        return 0
    try:
        with open(legacy_index, encoding="utf-8") as fh:
            records = json.load(fh)
    except (OSError, ValueError):
        return 0
    if not isinstance(records, list):
        return 0

    target_dir = _subject_dir(subject)
    _ensure_dir(target_dir)
    kept = []
    for record in records:
        if not isinstance(record, dict) or not record.get("id"):
            continue
        fname = f"{record['id']}.pdf"
        src = os.path.join(base_dir(), record.get("file") or "")
        try:
            if record.get("file") and os.path.exists(src):
                os.replace(src, os.path.join(target_dir, fname))
            record["file"] = fname
            record.setdefault("status", STATUS_OK)
            kept.append(record)
        except OSError:
            continue
    if not kept:
        return 0
    _write_index(subject, kept[:MAX_RECORDS])
    try:
        os.remove(legacy_index)
    except OSError:
        pass
    return len(kept)
