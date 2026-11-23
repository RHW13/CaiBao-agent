"""用户身份识别：账号登录态 + 设备唯一标识，双重维度确定数据归属主体。

主体规则：
  - 已登录：subject = user:<用户名>
  - 未登录：subject = device:<设备ID>

主体标识经 SHA1 摘要后作为存储目录名，既避免用户名 / 设备 ID 明文落盘，
也从根源上杜绝 ``../`` 之类的路径穿越。密码使用 PBKDF2-HMAC-SHA256 加盐存储，
登录令牌随机生成并带过期时间，均不保存明文。
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SESSION_TTL = 30 * 24 * 3600        # 登录态有效期：30 天
_ITERATIONS = 200_000
_SUBJECT_RE = re.compile(r"^[ud]_[0-9a-f]{24}$")


def base_dir() -> str:
    """身份与历史数据的根目录；可用环境变量覆盖（测试隔离用）。"""
    return os.environ.get("CB_HISTORY_DIR") or os.path.join(_ROOT, "data", "uploads")


def _accounts_path() -> str:
    return os.path.join(base_dir(), "accounts.json")


def _sessions_path() -> str:
    return os.path.join(base_dir(), "sessions.json")


def _load(path: str, default):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return default
    return data if isinstance(data, type(default)) else default


def _save(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def norm_username(username: str) -> str:
    """用户名规范化：去空格并统一小写，保证同一账号稳定映射到同一主体。"""
    return (username or "").strip().lower()


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", (password or "").encode("utf-8"), bytes.fromhex(salt), _ITERATIONS
    ).hex()


def register(username: str, password: str):
    """注册账号，返回 (是否成功, 提示或令牌)。"""
    name = norm_username(username)
    if not name or len(name) > 40:
        return False, "用户名需为 1–40 个字符。"
    if not re.fullmatch(r"[\w.\-@\u4e00-\u9fa5]+", name):
        return False, "用户名只能包含字母、数字、下划线、点、短横线、@ 或中文。"
    if len(password or "") < 6:
        return False, "密码至少 6 位。"
    accounts = _load(_accounts_path(), {})
    if name in accounts:
        return False, "该用户名已被注册。"
    salt = secrets.token_hex(16)
    accounts[name] = {
        "salt": salt,
        "hash": _hash_password(password, salt),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    _save(_accounts_path(), accounts)
    return True, "注册成功，请登录。"


def login(username: str, password: str):
    """校验账号密码，成功时签发并返回登录令牌。"""
    name = norm_username(username)
    rec = _load(_accounts_path(), {}).get(name)
    if not rec:
        return False, "用户名或密码错误。"
    try:
        expected = _hash_password(password, rec.get("salt", ""))
    except ValueError:
        return False, "用户名或密码错误。"
    if not hmac.compare_digest(rec.get("hash", ""), expected):
        return False, "用户名或密码错误。"
    now = time.time()
    sessions = {
        tok: s for tok, s in _load(_sessions_path(), {}).items()
        if isinstance(s, dict) and s.get("expires", 0) > now
    }
    token = secrets.token_urlsafe(32)
    sessions[token] = {"user": name, "expires": now + SESSION_TTL}
    _save(_sessions_path(), sessions)
    return True, token


def logout(token: str) -> None:
    if not token:
        return
    sessions = _load(_sessions_path(), {})
    if sessions.pop(token, None) is not None:
        _save(_sessions_path(), sessions)


def user_from_token(token: str):
    """令牌有效时返回用户名，否则返回 None（过期 / 伪造 / 已登出）。"""
    if not token:
        return None
    rec = _load(_sessions_path(), {}).get(token)
    if not isinstance(rec, dict) or rec.get("expires", 0) <= time.time():
        return None
    return rec.get("user")


def subject(username: str = None, device_id: str = None) -> str:
    """返回可用于目录名的稳定主体标识（登录优先，否则用设备）。"""
    if username:
        raw, prefix = "user:" + norm_username(username), "u"
    else:
        raw, prefix = "device:" + (device_id or "anonymous"), "d"
    return prefix + "_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def is_valid_subject(value: str) -> bool:
    return bool(isinstance(value, str) and _SUBJECT_RE.match(value))


def subject_label(username: str = None, device_id: str = None) -> str:
    if username:
        return f"账号 {norm_username(username)}"
    if device_id:
        return f"设备 {device_id[:8]}"
    return "访客设备"
