from __future__ import annotations

import base64
import json
import logging
import typing as t
from dataclasses import dataclass
from functools import cached_property
from io import BufferedIOBase, BytesIO
from typing import TYPE_CHECKING

import requests
from PIL import Image
from requests.adapters import HTTPAdapter

logger = logging.getLogger("iris.models")

if TYPE_CHECKING:
    from iris.bot._internal import IrisAPI

# 모듈 공유 HTTP 세션 (이미지·긴 메시지 fetch용)
_http_session = requests.Session()
_http_session.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=16))
_http_session.mount("http://",  HTTPAdapter(pool_connections=4, pool_maxsize=16))


def _fetch_image(url: str) -> t.Optional[Image.Image]:
    logger.debug("이미지 fetch: %s", url)
    try:
        img = Image.open(BytesIO(_http_session.get(url, timeout=10).content))
        result = img.convert("RGBA")
        logger.debug("이미지 fetch 성공: %s %s", url, result.size)
        return result
    except Exception as e:
        logger.warning("이미지 fetch 실패: %s — %s", url, e)
        return None


# ── Message ───────────────────────────────────────────────────────────────────

@dataclass
class Message:
    id: t.Union[int, str]
    type: t.Optional[int]
    msg: str
    attachment: t.Optional[str]
    v: t.Optional[dict]
    is_lite: bool = False

    def __post_init__(self):
        self.command, *param = self.msg.split(" ", 1)
        self.has_param = bool(param)
        self.param = param[0] if self.has_param else None

        logger.debug(
            "Message 파싱: id=%s type=%s command=%r has_param=%s is_lite=%s",
            self.id, self.type, self.command, self.has_param, self.is_lite,
        )

        if self.is_lite:
            self.image = None
            return

        try:
            self.attachment = json.loads(self.attachment)
        except Exception:
            pass

        if self.type in [71, 27, 2, 71 + 16384, 27 + 16384, 2 + 16384]:
            logger.debug("이미지 메시지 감지: type=%s", self.type)
            self.image = ChatImage(self)
        else:
            self.image = None

        if (
            len(self.msg) >= 3900
            and isinstance(self.attachment, dict)
            and "path" in self.attachment
        ):
            logger.debug("긴 메시지 fetch: path=%s", self.attachment["path"])
            try:
                res = _http_session.get(
                    "https://dn-m.talk.kakao.com/" + self.attachment["path"], timeout=10
                )
                res.encoding = "utf-8"
                self.msg = res.text
                logger.debug("긴 메시지 fetch 완료: %d chars", len(self.msg))
            except Exception as e:
                logger.warning("긴 메시지 fetch 실패: %s", e)

    def __repr__(self) -> str:
        return f"Message(id={self.id}, type={self.type}, msg={self.msg})"


# ── Room ──────────────────────────────────────────────────────────────────────

class Room:
    def __init__(
        self,
        id: t.Union[int, str],
        name: str,
        api: IrisAPI,
        is_lite: bool = False,
        is_group_chat: bool = False,
    ):
        self.id = id
        self.name = name
        self._api = api
        self.is_lite = is_lite
        self._is_group_chat = is_group_chat
        logger.debug("Room: id=%s name=%r is_lite=%s", id, name, is_lite)

    @property
    def is_group_chat(self) -> bool:
        return self._is_group_chat

    @cached_property
    def type(self) -> t.Optional[str]:
        if self.is_lite:
            return None
        logger.debug("Room.type 조회: id=%s", self.id)
        try:
            results = self._api.query("SELECT type FROM chat_rooms WHERE id = ?", [self.id])
            t_ = results[0].get("type") if results else None
            logger.debug("Room.type = %r", t_)
            return t_
        except Exception as e:
            logger.warning("Room.type 조회 실패: %s", e)
            return None

    def __repr__(self) -> str:
        return f"Room(id={self.id}, name={self.name})"


# ── User ──────────────────────────────────────────────────────────────────────

class User:
    def __init__(
        self,
        id: t.Union[int, str],
        chat_id: t.Union[int, str],
        api: IrisAPI,
        name: str = None,
        bot_id: int = None,
        is_lite: bool = False,
        profile_image: str = None,
    ):
        self.id = id
        self._chat_id = chat_id
        self._api = api
        self._name = name
        self._bot_id = bot_id
        self.is_lite = is_lite
        self.avatar = Avatar(id, chat_id, api, is_lite=is_lite, profile_image=profile_image)
        logger.debug("User: id=%s name=%r is_lite=%s", id, name, is_lite)

    @cached_property
    def name(self) -> t.Optional[str]:
        if self.is_lite or self._name:
            return self._name
        logger.debug("User.name DB 조회: id=%s bot_id=%s", self.id, self._bot_id)
        try:
            if self.id == self._bot_id:
                q = "SELECT T2.nickname FROM chat_rooms AS T1 JOIN db2.open_profile AS T2 ON T1.link_id = T2.link_id WHERE T1.id = ?"
                name = self._api.query(q, [self._chat_id])[0].get("nickname")
            elif self.id < 10_000_000_000:
                q = "SELECT name, enc FROM db2.friends WHERE id = ?"
                row = self._api.query(q, [self.id])[0]
                name = self._decrypt_name(row.get("name"), row.get("enc"))
            else:
                q = "SELECT nickname, enc FROM db2.open_chat_member WHERE user_id = ?"
                row = self._api.query(q, [self.id])[0]
                name = self._decrypt_name(row.get("nickname"), row.get("enc"))
            logger.debug("User.name = %r (id=%s)", name, self.id)
            return name
        except Exception as e:
            logger.warning("User.name 조회 실패 (id=%s): %s", self.id, e)
            return None

    def _decrypt_name(self, name: str | None, enc) -> str | None:
        if not name:
            return name
        try:
            enc_value = int(enc or 0)
        except Exception:
            enc_value = 0
        if enc_value == 0:
            return name
        try:
            decrypted = self._api.decrypt(enc_value, name, self.id)
            if decrypted:
                logger.debug("이름 복호화 성공: enc=%d id=%s", enc_value, self.id)
                return decrypted
        except Exception as e:
            logger.debug("이름 복호화 실패 (enc=%d id=%s): %s", enc_value, self.id, e)
        return name

    @cached_property
    def type(self) -> t.Optional[str]:
        if self.is_lite:
            return None
        logger.debug("User.type 조회: id=%s", self.id)
        try:
            if self.id == self._bot_id:
                q = "SELECT T2.link_member_type FROM chat_rooms AS T1 INNER JOIN open_profile AS T2 ON T1.link_id = T2.link_id WHERE T1.id = ?"
                results = self._api.query(q, [self._chat_id])
            else:
                q = "SELECT link_member_type FROM db2.open_chat_member WHERE user_id = ?"
                results = self._api.query(q, [self.id])
            match int(results[0].get("link_member_type")):
                case 1: t_ = "HOST"
                case 2: t_ = "NORMAL"
                case 4: t_ = "MANAGER"
                case 8: t_ = "BOT"
                case _: t_ = "UNKNOWN"
            logger.debug("User.type = %s (id=%s)", t_, self.id)
            return t_
        except Exception as e:
            logger.debug("User.type 조회 실패 (id=%s): %s → REAL_PROFILE", self.id, e)
            return "REAL_PROFILE"

    def __repr__(self) -> str:
        return f"User(id={self.id}, name={self.name})"


# ── Avatar ────────────────────────────────────────────────────────────────────

class Avatar:
    def __init__(
        self,
        id: t.Union[int, str],
        chat_id: t.Union[int, str],
        api: IrisAPI,
        is_lite: bool = False,
        profile_image: str = None,
    ):
        self._id = id
        self._chat_id = chat_id
        self._api = api
        self.is_lite = is_lite
        self._profile_image = profile_image

    @cached_property
    def url(self) -> t.Optional[str]:
        if self.is_lite:
            return None
        logger.debug("Avatar.url 조회: user_id=%s", self._id)
        try:
            if self._id < 10_000_000_000:
                q = "SELECT T2.o_profile_image_url FROM chat_rooms AS T1 JOIN db2.open_profile AS T2 ON T1.link_id = T2.link_id WHERE T1.id = ?"
                results = self._api.query(q, [self._chat_id])
                url = results[0].get("o_profile_image_url")
            else:
                q = "SELECT original_profile_image_url, enc FROM db2.open_chat_member WHERE user_id = ?"
                results = self._api.query(q, [self._id])
                if not results:
                    return None
                row = results[0]
                url = self._decrypt_avatar_url(row.get("original_profile_image_url"), row.get("enc"))
            logger.debug("Avatar.url = %s", url)
            return url
        except Exception as e:
            logger.warning("Avatar.url 조회 실패 (id=%s): %s", self._id, e)
            return None

    def _decrypt_avatar_url(self, url: str | None, enc: int | str | None) -> str | None:
        if not url:
            return None
        try:
            enc_value = int(enc or 0)
        except Exception:
            enc_value = 0
        if enc_value:
            logger.debug("아바타 URL 복호화: enc=%d", enc_value)
            try:
                decrypted = self._api.decrypt(enc_value, url, self._id)
                if decrypted:
                    return decrypted
            except Exception as e:
                logger.warning("아바타 URL 복호화 실패: %s", e)
        return url

    @cached_property
    def img(self) -> t.Optional[Image.Image]:
        if self.is_lite:
            if self._profile_image:
                try:
                    return Image.open(BytesIO(base64.b64decode(self._profile_image)))
                except Exception as e:
                    logger.warning("lite 프로필 이미지 디코딩 실패: %s", e)
                    return None
            return None
        return _fetch_image(self.url) if self.url else None

    def __repr__(self) -> str:
        return f"Avatar(url={self.url})"


# ── ChatImage ─────────────────────────────────────────────────────────────────

class ChatImage:
    def __init__(self, message: Message):
        self.url = self._get_photo_url(message)

    @cached_property
    def img(self) -> t.Optional[t.List[Image.Image]]:
        if not self.url:
            return None
        logger.debug("ChatImage.img: %d개 URL fetch", len(self.url))
        try:
            return [_fetch_image(url) for url in self.url]
        except Exception as e:
            logger.warning("ChatImage.img 실패: %s", e)
            return None

    def _get_photo_url(self, message: Message) -> t.Optional[t.List[str]]:
        try:
            if message.type == 71:
                urls = []
                for item in message.attachment["C"]["THL"]:
                    th = item.get("TH", {})
                    # 원본 > 중간 > 썸네일 순으로 사용 가능한 URL 선택
                    url = th.get("TU") or th.get("MU") or th.get("THU")
                    if url:
                        urls.append(url)
                return urls or None
            if message.type == 27:
                return list(message.attachment["imageUrls"])
            return [message.attachment["url"]]
        except Exception as e:
            logger.debug("ChatImage URL 파싱 실패: %s", e)
            return None

    def __repr__(self) -> str:
        return f"ChatImage(url={self.url})"


# ── ChatContext ───────────────────────────────────────────────────────────────

@dataclass
class ChatContext:
    room: Room
    sender: User
    message: Message
    raw: dict
    api: IrisAPI
    _bot_id: int = None
    is_lite: bool = False

    def _resolve_thread_id(self, thread_id: int | None, use_thread: bool) -> int | None:
        if not use_thread:
            return thread_id
        scope = self.raw.get("scope")
        # scope=3(전체) -> 일반 답장(스레드 금지), scope=2(답글) -> 스레드 유지
        if scope == 3 or scope == 1:
            return None
        if scope == 2 and thread_id is None and self.raw.get("thread_id"):
            try:
                return int(self.raw["thread_id"])
            except (ValueError, TypeError):
                return None
        if thread_id is None and self.raw.get("thread_id"):
            try:
                return int(self.raw["thread_id"])
            except (ValueError, TypeError):
                return None
        return thread_id

    def reply(
        self,
        message: str,
        room_id: int = None,
        thread_id: int = None,
        use_thread: bool = True,
        attachment: str = None,
        markdown: bool = False,
        broadcast_scope: bool = True,
    ):
        if room_id is None:
            room_id = self.room.id
        if markdown and attachment is None:
            attachment = '{"markdown":true}'
        if attachment is not None:
            try:
                json.loads(attachment)
            except (json.JSONDecodeError, ValueError):
                try:
                    attachment = attachment.replace("'", '"')
                    json.loads(attachment)
                except Exception:
                    pass

        # scope=3(전체)이고 thread_id가 있으면 thread와 메인 채팅 양쪽에 전송
        if use_thread and broadcast_scope and isinstance(self.raw, dict) and self.raw.get("scope") == 3:
            raw_tid = thread_id if thread_id is not None else self.raw.get("thread_id")
            if raw_tid is not None:
                try:
                    raw_tid = int(raw_tid)
                    logger.debug("scope=3 broadcast: room=%s thread=%s", room_id, raw_tid)
                    self.api.reply(room_id, message, thread_id=raw_tid, attachment=attachment)
                    self.api.reply(room_id, message, thread_id=None, attachment=attachment)
                    return
                except (TypeError, ValueError):
                    pass

        thread_id = self._resolve_thread_id(thread_id, use_thread)
        logger.debug("reply: room=%s thread=%s msg=%.60r", room_id, thread_id, message)
        try:
            self.api.reply(room_id, message, thread_id=thread_id, attachment=attachment)
        except Exception as e:
            logger.error("reply 실패: %s", e)

    def reply_file(self, file_path: str, room_id: int = None, attachment: str = None):
        if room_id is None:
            room_id = self.room.id
        logger.debug("reply_file: room=%s path=%s attachment=%s", room_id, file_path, attachment)
        try:
            self.api.reply_file(room_id, file_path, attachment=attachment)
        except Exception as e:
            logger.error("reply_file 실패: %s", e)

    def reply_media(
        self,
        files: t.List[BufferedIOBase | bytes | Image.Image | str],
        room_id: int = None,
        thread_id: int = None,
        use_thread: bool = True,
        broadcast_scope: bool = True,
    ):
        if room_id is None:
            room_id = self.room.id
        thread_id = self._resolve_thread_id(thread_id, use_thread)
        logger.debug("reply_media: room=%s thread=%s files=%d개", room_id, thread_id, len(files) if isinstance(files, list) else 1)
        self.api.reply_media(room_id, files, thread_id=thread_id)

    # ── 채팅 탐색 ──────────────────────────────────────────────────────────────

    def get_source(self) -> t.Optional[ChatContext]:
        if self.is_lite:
            return None
        logger.debug("get_source: msg_id=%s", self.message.id)
        record = self._get_reply_record(self.message)
        return self._make_chat(record) if record else None

    def get_next_chat(self, n: int = 1) -> t.Optional[ChatContext]:
        if self.is_lite:
            return None
        logger.debug("get_next_chat: n=%d msg_id=%s", n, self.message.id)
        record = self._get_next_record(self.message.id, n)
        return self._make_chat(record) if record else None

    def get_previous_chat(self, n: int = 1) -> t.Optional[ChatContext]:
        if self.is_lite:
            return None
        logger.debug("get_previous_chat: n=%d msg_id=%s", n, self.message.id)
        record = self._get_previous_record(self.message.id, n)
        return self._make_chat(record) if record else None

    def _get_reply_record(self, message: Message):
        try:
            src_log_id = message.attachment["src_logId"]
            records = self.api.query("SELECT * FROM chat_logs WHERE id = ?", [src_log_id])
            return records[0] if records else None
        except Exception as e:
            logger.debug("get_source 실패: %s", e)
            return None

    def _get_previous_record(self, log_id, n: int = 1):
        if n < 0:
            raise ValueError("n must be greater than 0")
        query = """
            WITH RECURSIVE ChatHistory AS (
                SELECT * FROM chat_logs WHERE id = ?
                UNION ALL
                SELECT c.* FROM chat_logs c JOIN ChatHistory h ON c.id = h.prev_id
            )
            SELECT * FROM ChatHistory LIMIT 1 OFFSET ?;
        """
        records = self.api.query(query, [log_id, n])
        return records[0] if records else None

    def _get_next_record(self, log_id, n: int = 1):
        n -= 1
        if n < -1:
            raise ValueError("n must be greater than 0")
        query = """
            WITH RECURSIVE ChatHistory AS (
                SELECT *, 0 AS depth FROM chat_logs WHERE id = ?
                UNION ALL
                SELECT c.*, h.depth + 1 FROM chat_logs c
                JOIN ChatHistory h ON c.prev_id = h.id
                WHERE h.depth < 100 AND c.prev_id IS NOT NULL AND h.id IS NOT NULL AND c.id IS NOT NULL
            )
            SELECT * FROM ChatHistory WHERE depth = ? + 1 LIMIT 1;
        """
        records = self.api.query(query, [log_id, n])
        return records[0] if records else None

    def _make_chat(self, record: dict) -> ChatContext:
        v = {}
        try:
            v = json.loads(record["v"])
        except Exception:
            pass
        room = Room(id=int(record["chat_id"]), name=self.room.name, api=self.api)
        sender = User(
            id=int(record["user_id"]),
            chat_id=self.room.id,
            api=self.api,
            name=self._name_of(int(record["user_id"])),
            bot_id=self._bot_id,
        )
        message = Message(
            id=int(record["id"]),
            type=int(record["type"]),
            msg=record["message"],
            attachment=record["attachment"],
            v=v,
        )
        return ChatContext(
            room=room, sender=sender, message=message,
            raw=record, api=self.api, _bot_id=self._bot_id,
        )

    def _name_of(self, user_id: int) -> t.Optional[str]:
        query = (
            "WITH info AS (SELECT ? AS user_id) "
            "SELECT COALESCE(open_chat_member.nickname, friends.name) AS name "
            "FROM info "
            "LEFT JOIN db2.open_chat_member ON open_chat_member.user_id = info.user_id "
            "LEFT JOIN db2.friends ON friends.id = info.user_id;"
        )
        result = self.api.query(query, [user_id])
        return result[0]["name"] if result else None

    # ── 수정 이력 ──────────────────────────────────────────────────────────────

    def fetch_revised_text(self, original_id: int = None) -> dict | str:
        """수정된 메시지의 원문과 전체 수정 이력을 반환한다."""
        api = self.api
        if original_id is None:
            try:
                original_id = json.loads(self.message.msg)["logId"]
            except Exception:
                original_id = self.message.id

        logger.debug("fetch_revised_text: original_id=%s", original_id)
        try:
            result = api.query("SELECT * FROM chat_logs WHERE id = ?", [original_id])
            record = result[0]
        except Exception as e:
            logger.error("메시지 조회 실패 (id=%s): %s", original_id, e)
            return "(내용 조회 실패)"

        msg_id, user_id = str(record["id"]), int(record["user_id"])
        try:
            v = json.loads(record.get("v") or "{}")
        except Exception:
            v = {}

        modify_revision = int(v.get("modifyRevision", 0))
        enc_default = int(v.get("enc", 0))
        logger.debug("modify_revision=%d enc=%d", modify_revision, enc_default)

        def decrypt(b64: str, enc: int) -> str:
            b64 = b64.replace("\\/", "/").replace("\\\\", "\\")
            if not b64 or enc == 0:
                return b64
            try:
                return api.decrypt(enc, b64, user_id) or b64
            except Exception:
                return b64

        def db_get(rev):
            try:
                rows = api.query(
                    "SELECT message FROM revision_cache WHERE msg_id=? AND revision=?",
                    [msg_id, rev],
                )
                return rows[0]["message"] if rows else None
            except Exception:
                return None

        def db_set(rev, text):
            try:
                api.query(
                    "INSERT OR REPLACE INTO revision_cache (msg_id,revision,message) VALUES (?,?,?)",
                    [msg_id, rev, text],
                )
            except Exception:
                pass

        try:
            api.query(
                "CREATE TABLE IF NOT EXISTS revision_cache "
                "(msg_id TEXT, revision INTEGER, message TEXT, "
                "created_at INTEGER DEFAULT (strftime('%s','now')), "
                "PRIMARY KEY(msg_id,revision))",
                [],
            )
        except Exception:
            pass

        if modify_revision == 0 or "modifyLog" not in v:
            return record

        try:
            raw_log = (
                json.loads(v["modifyLog"].replace("\\/", "/").replace("\\\\", "\\"))
                if isinstance(v["modifyLog"], str)
                else v["modifyLog"]
            )
        except Exception:
            raw_log = []

        for e in raw_log:
            db_set(int(e["revision"]), decrypt(e["message"], int(e.get("enc", enc_default))))

        cur_text = record.get("message", "")
        db_set(modify_revision, cur_text)
        v["modifyLog"] = [
            {"revision": r, "message": db_get(r), "enc": 0}
            for r in range(modify_revision + 1)
        ]
        record["message"] = cur_text
        record["v"] = json.dumps(v, ensure_ascii=False)
        logger.debug("fetch_revised_text 완료: revision=%d", modify_revision)
        return record


# ── ErrorContext ──────────────────────────────────────────────────────────────

@dataclass
class ErrorContext:
    event: str
    func: t.Callable
    exception: Exception
    args: list[t.Any]
