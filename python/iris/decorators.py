from __future__ import annotations
import time
from functools import wraps
from iris import ChatContext, PyKV
from utils.config import load_config

_config = load_config("config.json")

# 역할 레벨: user < manager < owner
_ROLE_LEVEL: dict[str, int] = {"user": 0, "manager": 1, "owner": 2}

# KV 리스트 TTL 캐시 (managers, banned — 메시지마다 DB 조회 방지)
_KV_CACHE_TTL = 10.0
_kv_cache: dict[str, tuple[float, list]] = {}


def _cached_list(key: str) -> list:
    entry = _kv_cache.get(key)
    if entry and time.monotonic() - entry[0] < _KV_CACHE_TTL:
        return entry[1]
    val = PyKV().get(key) or []
    _kv_cache[key] = (time.monotonic(), val)
    return val


def invalidate_cache(key: str) -> None:
    _kv_cache.pop(key, None)


# ── 역할 조회 ──────────────────────────────────────────────────────────────────

def get_role(chat: ChatContext) -> str:
    """'owner' | 'manager' | 'user' 반환"""
    try:
        sender_id = int(chat.sender.id)
    except (ValueError, TypeError):
        sender_id = chat.sender.id
    owners = [int(o) for o in (_config.get("system.super_admin") or [])]
    if sender_id in owners:
        return "owner"
    managers = [int(m) for m in _cached_list("managers")]
    if sender_id in managers:
        return "manager"
    return "user"


def has_role(chat: ChatContext, min_role: str) -> bool:
    return _ROLE_LEVEL.get(get_role(chat), 0) >= _ROLE_LEVEL.get(min_role, 0)


# ── 역할 데코레이터 ────────────────────────────────────────────────────────────

def is_owner(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        chat: ChatContext = args[0]
        return func(*args, **kwargs) if has_role(chat, "owner") else None
    return wrapper


def is_manager(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        chat: ChatContext = args[0]
        return func(*args, **kwargs) if has_role(chat, "manager") else None
    return wrapper


# 하위 호환 별칭
is_admin = is_manager
is_super_admin = is_owner


# ── 유틸 데코레이터 ────────────────────────────────────────────────────────────

def has_param(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        chat: ChatContext = args[0]
        return func(*args, **kwargs) if chat.message.has_param else None
    return wrapper


def is_reply(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        chat: ChatContext = args[0]
        att = chat.message.attachment
        if chat.message.type == 26 or (isinstance(att, dict) and att.get("src_isThread")):
            return func(*args, **kwargs)
        chat.reply("메시지에 답장하여 요청하세요.")
        return None
    return wrapper


def is_not_banned(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        chat: ChatContext = args[0]
        return None if chat.sender.id in _cached_list("banned") else func(*args, **kwargs)
    return wrapper


# ── 인라인 체크 함수 ───────────────────────────────────────────────────────────

def admin_check(chat: ChatContext) -> bool:
    return has_role(chat, "manager")


def super_admin_check(chat: ChatContext) -> bool:
    return has_role(chat, "owner")
