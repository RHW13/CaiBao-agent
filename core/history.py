"""上传历史：在本机持久化最近 12 份财报，供侧边栏回看与快速切换。

每份记录包含原始 PDF 文件与分析元信息（公司、报告期），存放在 data/uploads：
  - index.json  记录列表（按上传时间倒序）
  - <id>.pdf    原始文件，删除记录时同步删除
同一文件按内容哈希去重，重复上传不会产生新记录，也不会改变首次上传时间。
"""
import hashlib
import json
import os
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("CB_HISTORY_DIR") or os.path.join(_ROOT, "data", "uploads")
INDEX = os.path.join(DATA_DIR, "index.json")
MAX_RECORDS = 12


def _ensure_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def _read_index() -> list:
    try:
        with open(INDEX, encoding="utf-8") as fh:
            records = json.load(fh)
    except (OSError, ValueError):
        return []
    if not isinstance(records, list):
        return []
    return [r for r in records if isinstance(r, dict) and r.get("id")]


def _write_index(records: list) -> None:
    """原子写入，避免进程中断留下半个 JSON 文件。"""
    _ensure_dir()
    tmp = INDEX + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(records, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, INDEX)


def _remove_file(record: dict) -> None:
    path = os.path.join(DATA_DIR, record.get("file") or "")
    try:
        if record.get("file") and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def list_records(limit: int = MAX_RECORDS) -> list:
    """按上传时间倒序返回记录；最多 MAX_RECORDS 条。"""
    return _read_index()[:limit]


def get_record(rid: str):
    return next((r for r in _read_index() if r.get("id") == rid), None)


def load_bytes(rid: str):
    """读取原始 PDF 内容；文件缺失时返回 None。"""
    record = get_record(rid)
    if not record or not record.get("file"):
        return None
    try:
        with open(os.path.join(DATA_DIR, record["file"]), "rb") as fh:
            return fh.read()
    except OSError:
        return None


def add_record(name: str, blob: bytes, meta: dict = None) -> dict:
    """保存一次上传；同内容文件只保留首次记录。"""
    digest = hashlib.sha1(blob).hexdigest()
    records = _read_index()
    for record in records:
        if record.get("hash") == digest:
            return record

    rid = digest[:12]
    fname = f"{rid}.pdf"
    _ensure_dir()
    with open(os.path.join(DATA_DIR, fname), "wb") as fh:
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
    }
    records.insert(0, record)
    for old in records[MAX_RECORDS:]:
        _remove_file(old)
    _write_index(records[:MAX_RECORDS])
    return record


def delete_record(rid: str) -> bool:
    """删除记录并同步删除本地 PDF 文件。"""
    records = _read_index()
    target = next((r for r in records if r.get("id") == rid), None)
    if target is None:
        return False
    _remove_file(target)
    _write_index([r for r in records if r.get("id") != rid])
    return True


def clear_all() -> int:
    """清空全部记录并删除本地 PDF 文件，返回清除条数。"""
    records = _read_index()
    for record in records:
        _remove_file(record)
    _write_index([])
    return len(records)
