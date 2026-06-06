from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import queue
import threading
import time
import typing as t
from urllib.parse import urlparse
from typing import TYPE_CHECKING

from websockets.sync.client import connect

from iris.bot.models import ChatContext, Message, Room, User
from iris.pykv import PyKV

if TYPE_CHECKING:
    from iris.bot._internal import EventEmitter, IrisAPI, IrisRequest

logger = logging.getLogger("iris.bot")

_ROLE_LEVEL: dict[str, int] = {"user": 0, "manager": 1, "owner": 2}


class Bot:
    _ORIGIN_EVENT: dict[str, str] = {
        "MSG":        "message",
        "NEWMEM":     "new_member",
        "DELMEM":     "del_member",
        "WRITE":      "msg_write",
        "MODIFYMSG":  "msg_edited",
        "SYNCMODMSG": "msg_edited",
        "DELETEMSG":  "msg_deleted",
    }

    def __init__(self, iris_url: str, *, prefix: str = "!", max_workers: int = 32):
        self.iris_url = self._normalize_url(iris_url)
        self.iris_ws_endpoint = f"ws://{self.iris_url}/ws"
        from iris.bot._internal import EventEmitter, IrisAPI

        self.api = IrisAPI(f"http://{self.iris_url}")
        self.emitter = EventEmitter(max_workers=max_workers)
        self.bot_id: int | None = None
        self._routes: dict[str, dict[str, t.Callable]] = {}
        self._command_groups: list[dict] = []   # {cmds, help}
        self.default_prefix = prefix
        self.prefix = prefix
        self._kv = PyKV()
        self._room_queues: dict[int, queue.SimpleQueue] = {}
        self._room_threads: dict[int, threading.Thread] = {}
        self._room_lock = threading.Lock()
        self._on_connect_cbs: list[t.Callable] = []
        logger.debug("Bot 초기화: %s (prefix=%r max_workers=%d)", self.iris_url, prefix, max_workers)

    # ── URL 정규화 ─────────────────────────────────────────────────────────────

    @staticmethod
    def _normalize_url(raw: str) -> str:
        if "://" not in raw:
            raw = "//" + raw
        parsed = urlparse(raw)
        host, port = parsed.hostname, parsed.port
        if not host or not port:
            raise ValueError(
                "Iris endpoint 주소는 IP:PORT 형식이어야 합니다. ex) 127.0.0.1:3000"
            )
        result = f"{host}:{port}"
        logger.debug("URL 정규화: %r → %s", raw, result)
        return result

    # ── on_connect 콜백 ────────────────────────────────────────────────────────

    def on_connect(self, func: t.Callable) -> t.Callable:
        """WebSocket 연결 직후 호출될 콜백을 등록한다."""
        self._on_connect_cbs.append(func)
        return func

    # ── 이벤트 데코레이터 ──────────────────────────────────────────────────────

    def on_event(self, name: str) -> t.Callable:
        def decorator(func: t.Callable) -> t.Callable:
            self.emitter.register(name, func)
            return func
        return decorator

    def on_command(
        self,
        *commands: str,
        help: str = None,
        usage: str = None,
        kv: str = None,
        role: str = None,
        section: str = None,
        events: t.Sequence[str] = ("message",),
    ) -> t.Callable:
        """
        지정 이벤트에서 command가 일치할 때 호출되는 핸들러를 등록한다.
        명령어는 '핑' 또는 '!핑' 형식 모두 지원 (내부적으로 '!'로 정규화됨)

            @bot.on_command("핑", "ping", help="응답 테스트", section="기본")
            def cmd_ping(chat: ChatContext):
                chat.reply("퐁!")
        """
        def decorator(func: t.Callable) -> t.Callable:
            normalized_commands = []
            for cmd in commands:
                # prefix 없으면 ! 추가
                if not cmd.startswith(('!', '/', '.', '#')):
                    cmd = '!' + cmd
                normalized_commands.append(cmd)

            for event in events:
                for cmd in normalized_commands:
                    self._routes.setdefault(event, {})[cmd] = func
                    logger.debug("커맨드 등록: event=%s cmd=%r func=%s", event, cmd, func.__name__)
            if help is not None:
                self._command_groups.append({
                    "cmds": list(normalized_commands),
                    "help": help,
                    "usage": usage,
                    "kv": kv,
                    "role": role,
                    "section": section,
                })
            return func
        return decorator

    # ── 방 설정 헬퍼 ───────────────────────────────────────────────────────────

    def get_room_prefix(self, room_id) -> str:
        """방의 커맨드 prefix를 반환한다. 설정이 없으면 default_prefix를 반환."""
        return self._kv.get(f"room:{room_id}:prefix") or self.default_prefix

    def set_room_prefix(self, room_id, prefix: str) -> None:
        self._kv.put(f"room:{room_id}:prefix", prefix)
        logger.debug("방 prefix 설정: room=%s prefix=%r", room_id, prefix)

    def _normalize_prefix_list(self, value) -> list[str]:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return []
        cleaned: list[str] = []
        for item in value:
            prefix = str(item).strip()
            if prefix and prefix not in cleaned:
                cleaned.append(prefix)
        return cleaned

    def get_user_prefixes(self, user_id) -> list[str]:
        """사용자의 prefix 후보 목록을 반환한다. 최신 prefix가 앞에 온다."""
        current = self._kv.get(f"user:{user_id}:prefix")
        history = self._kv.get(f"user:{user_id}:prefix_history")
        prefixes = self._normalize_prefix_list(current)
        prefixes.extend(self._normalize_prefix_list(history))
        if self.default_prefix:
            prefixes.append(self.default_prefix)
        out: list[str] = []
        for prefix in prefixes:
            if prefix not in out:
                out.append(prefix)
        return out

    def reset_user_prefix(self, user_id) -> None:
        """사용자 prefix를 기본값으로 되돌린다."""
        self._kv.delete(f"user:{user_id}:prefix")
        self._kv.delete(f"user:{user_id}:prefix_history")
        logger.debug("사용자 prefix 초기화: user=%s", user_id)

    def get_user_command_aliases(self, user_id) -> dict[str, str]:
        value = self._kv.get(f"user:{user_id}:command_aliases")
        if not isinstance(value, dict):
            return {}
        aliases: dict[str, str] = {}
        for cmd, alias in value.items():
            cmd_s = str(cmd).strip()
            alias_s = str(alias).strip()
            if cmd_s and alias_s:
                aliases[cmd_s] = alias_s
        return aliases

    def get_user_command_alias(self, user_id, command: str) -> str | None:
        return self.get_user_command_aliases(user_id).get(str(command).strip())

    def set_user_command_alias(self, user_id, command: str, alias: str) -> None:
        command = str(command).strip()
        alias = str(alias).strip()
        if not command:
            return
        aliases = self.get_user_command_aliases(user_id)
        if alias:
            aliases[command] = alias
        else:
            aliases.pop(command, None)
        self._kv.put(f"user:{user_id}:command_aliases", aliases)
        logger.debug("사용자 command alias 설정: user=%s command=%s alias=%r", user_id, command, alias)

    def reset_user_command_alias(self, user_id, command: str) -> None:
        aliases = self.get_user_command_aliases(user_id)
        aliases.pop(str(command).strip(), None)
        self._kv.put(f"user:{user_id}:command_aliases", aliases)
        logger.debug("사용자 command alias 초기화: user=%s command=%s", user_id, command)

    def get_disabled_commands(self, room_id) -> set:
        return set(self._kv.get(f"room:{room_id}:disabled_cmds") or [])

    def disable_command(self, room_id, cmd: str) -> bool:
        """명령어를 비활성화. 이미 비활성화된 경우 False 반환."""
        disabled = list(self.get_disabled_commands(room_id))
        if cmd in disabled:
            return False
        disabled.append(cmd)
        self._kv.put(f"room:{room_id}:disabled_cmds", disabled)
        logger.debug("커맨드 비활성화: room=%s cmd=%r", room_id, cmd)
        return True

    def enable_command(self, room_id, cmd: str) -> bool:
        """명령어를 활성화. 이미 활성화된 경우 False 반환."""
        disabled = list(self.get_disabled_commands(room_id))
        if cmd not in disabled:
            return False
        disabled.remove(cmd)
        self._kv.put(f"room:{room_id}:disabled_cmds", disabled)
        logger.debug("커맨드 활성화: room=%s cmd=%r", room_id, cmd)
        return True

    def get_help_text(self, room_id=None, user_role: str = "user") -> str:
        """역할에 맞는 도움말 반환. 섹션별로 그룹핑, 마크다운 포맷."""
        user_level = _ROLE_LEVEL.get(user_role, 0)
        disabled = self.get_disabled_commands(room_id) if room_id is not None else set()

        # section → [(line, is_inactive)]
        from collections import OrderedDict
        sections: OrderedDict[str, list[str]] = OrderedDict()
        inactive: list[str] = []

        for group in self._command_groups:
            req = group.get("role")
            if req and _ROLE_LEVEL.get(req, 99) > user_level:
                continue

            cmds = group["cmds"]
            visible = [c for c in cmds if c not in disabled]
            cmd_strs = [f"**{c}**" for c in (visible if visible else cmds)]
            cmd_part = " / ".join(cmd_strs)
            if group.get("usage"):
                cmd_part += f" `{group['usage']}`"
            line = f"{cmd_part} — *{group['help']}*"

            sec = group.get("section") or "기타"
            if visible:
                sections.setdefault(sec, []).append(line)
            else:
                inactive.append(f"~~{line}~~")

        parts: list[str] = []
        for sec, lines in sections.items():
            parts.append(f"**— {sec} —**\n" + "\n".join(lines))
        if inactive:
            parts.append("**— 비활성 —**\n" + "\n".join(inactive))

        return "\n\n".join(parts) if parts else "등록된 도움말이 없습니다."

    # ── 커맨드 디스패치 ────────────────────────────────────────────────────────

    def get_user_prefix(self, user_id) -> str:
        """사용자의 커맨드 prefix를 반환한다. 설정이 없으면 default_prefix를 반환."""
        prefixes = self.get_user_prefixes(user_id)
        return prefixes[0] if prefixes else self.default_prefix

    def set_user_prefix(self, user_id, prefix: str) -> None:
        prefix = (prefix or "").strip()
        if not prefix:
            return
        current = self._kv.get(f"user:{user_id}:prefix")
        history = self._normalize_prefix_list(self._kv.get(f"user:{user_id}:prefix_history"))
        if isinstance(current, str) and current.strip() and current.strip() != prefix:
            history = [current.strip()] + [item for item in history if item != current.strip()]
        history = [item for item in history if item != prefix]
        self._kv.put(f"user:{user_id}:prefix", prefix)
        self._kv.put(f"user:{user_id}:prefix_history", history[:5])
        logger.debug("사용자 prefix 설정: user=%s prefix=%r", user_id, prefix)

    def _normalize_command(self, raw_cmd: str, user_id, room_id=None) -> list[str]:
        """현재 적용된 prefix(유저 > 방 > 기본)만 허용해 내부 '!' 명령으로 변환한다."""
        if not raw_cmd:
            return [raw_cmd]

        aliases = self.get_user_command_aliases(user_id)
        for command, alias in aliases.items():
            if alias and raw_cmd == alias:
                return [f"!{command}"]

        prefixes: list[str] = []
        prefixes.extend(self.get_user_prefixes(user_id))
        if room_id is not None:
            room_prefix = self.get_room_prefix(room_id)
            if room_prefix:
                prefixes.append(room_prefix)
        if self.default_prefix:
            prefixes.append(self.default_prefix)

        normalized: list[str] = []
        for prefix in self._normalize_prefix_list(prefixes):
            if prefix == "!" and raw_cmd.startswith("!"):
                normalized.append(raw_cmd)
            elif prefix and raw_cmd.startswith(prefix):
                normalized.append("!" + raw_cmd[len(prefix):])
        if normalized:
            return list(dict.fromkeys(normalized))

        # 설정된 prefix가 아니면 자동 후보 확장을 하지 않는다.
        return [raw_cmd]

    def _dispatch_command(self, event: str, chat: ChatContext) -> bool:
        raw_cmd = chat.message.command
        candidates = self._normalize_command(raw_cmd, chat.sender.id, chat.room.id)
        disabled = self.get_disabled_commands(chat.room.id)
        if any(cmd in disabled for cmd in candidates):
            logger.debug("커맨드 비활성화됨: room=%s cmd=%r", chat.room.id, raw_cmd)
            return False

        handler = None
        matched_cmd = None
        for cmd in candidates:
            handler = self._routes.get(event, {}).get(cmd)
            if handler:
                matched_cmd = cmd
                break

        if handler:
            logger.debug(
                "커맨드 매칭: event=%s cmd=%r → %s",
                event, matched_cmd, handler.__name__,
            )
            handler(chat)
            return True

        logger.debug("커맨드 미매칭: event=%s cmd=%r candidates=%s", event, raw_cmd, candidates)
        return False

    # ── WebSocket 수신 처리 ────────────────────────────────────────────────────

    def _build_chat(self, req: IrisRequest) -> ChatContext:
        logger.debug(
            "채팅 구성: room=%r sender=%r msg=%.60r is_lite=%s",
            req.room, req.sender, req.msg, req.is_lite,
        )
        v: dict = {}
        try:
            v = json.loads(req.raw["v"])
        except Exception:
            pass

        is_lite = req.is_lite
        room = Room(
            id=int(req.raw["chat_id"]),
            name=req.room,
            api=self.api,
            is_lite=is_lite,
            is_group_chat=req.raw.get("is_group_chat", False),
        )
        user_id = req.raw["user_id"]
        if not is_lite:
            user_id = int(user_id)

        sender = User(
            id=user_id,
            chat_id=room.id,
            api=self.api,
            name=req.sender,
            bot_id=self.bot_id,
            is_lite=is_lite,
            profile_image=req.raw.get("profile_image"),
        )
        message = Message(
            id=int(req.raw["id"]),
            type=int(req.raw["type"]) if req.raw.get("type") is not None else None,
            msg=req.raw["message"],
            attachment=req.raw["attachment"],
            v=v,
            is_lite=is_lite,
        )
        return ChatContext(
            room=room, sender=sender, message=message,
            raw=req.raw, api=self.api, _bot_id=self.bot_id, is_lite=is_lite,
        )

    # ── 방별 직렬 큐 ──────────────────────────────────────────────────────────

    def _get_room_queue(self, room_id) -> queue.SimpleQueue:
        if room_id not in self._room_queues:
            with self._room_lock:
                if room_id not in self._room_queues:
                    q: queue.SimpleQueue = queue.SimpleQueue()
                    t = threading.Thread(
                        target=self._room_worker,
                        args=(room_id, q),
                        daemon=True,
                        name=f"iris-room-{room_id}",
                    )
                    t.start()
                    self._room_queues[room_id] = q
                    self._room_threads[room_id] = t
                    logger.info("방 스레드 생성: room_id=%s", room_id)
        return self._room_queues[room_id]

    def _room_worker(self, room_id, q: queue.SimpleQueue) -> None:
        logger.debug("방 스레드 시작: room_id=%s", room_id)
        while True:
            task = q.get()
            if task is None:
                break
            try:
                task()
            except Exception as e:
                logger.error("방 %s 처리 오류: %s", room_id, e, exc_info=True)

    def _dispatch(self, chat: ChatContext) -> None:
        self._get_room_queue(chat.room.id).put(lambda: self._process_chat(chat))

    def _process_chat(self, chat: ChatContext) -> None:
        try:
            from modules.service_state import allow_message, service_down
            if service_down() and not allow_message(chat):
                logger.warning(
                    "서비스 차단 상태로 메시지 무시: room=%s msg=%r",
                    chat.room.id, getattr(chat.message, "msg", ""),
                )
                return
        except Exception:
            pass

        self.emitter.emit("chat", [chat])
        if chat.is_lite:
            logger.debug("lite 모드 메시지 → 'message' 이벤트")
            self.emitter.emit("message", [chat])
            return
        origin = chat.message.v.get("origin", "")
        event = self._ORIGIN_EVENT.get(origin, "unknown")
        logger.debug(
            "origin=%r → event=%s  room=%s msg_id=%s",
            origin, event, chat.room.id, chat.message.id,
        )
        self.emitter.emit(event, [chat])
        self._dispatch_command(event, chat)

    # ── 메인 루프 ──────────────────────────────────────────────────────────────

    def run(self) -> None:
        logger.info("Bot 시작: %s", self.iris_ws_endpoint)
        _backoff = 1.0
        while True:
            try:
                logger.info("웹소켓 연결 시도: %s", self.iris_ws_endpoint)
                with connect(
                    self.iris_ws_endpoint,
                    close_timeout=0,
                    max_size=10**6,
                ) as ws:
                    logger.info("웹소켓 연결됨")
                    _backoff = 1.0  # 연결 성공 시 backoff 초기화
                    try:
                        info = self.api.get_info()
                        self.bot_id = info["bot_id"]
                        logger.info("bot_id=%s", self.bot_id)
                    except Exception as e:
                        logger.warning("bot_id 조회 실패: %s", e)
                        self.bot_id = None

                    for cb in list(self._on_connect_cbs):
                        threading.Thread(target=cb, daemon=True).start()

                    while True:
                        recv = ws.recv()
                        logger.debug("ws 수신: %d bytes", len(recv))
                        try:
                            data: dict = json.loads(recv)
                            data["raw"] = data.pop("json")
                            from iris.bot._internal import IrisRequest
                            chat = self._build_chat(IrisRequest(**data))
                            self._dispatch(chat)
                        except Exception as e:
                            logger.error("이벤트 처리 중 오류: %s", e, exc_info=True)
            except KeyboardInterrupt:
                logger.info("Bot 종료 요청")
                for q in list(self._room_queues.values()):
                    q.put(None)
                self.emitter.shutdown()
                break
            except Exception as e:
                logger.error("웹소켓 오류: %s", e)
                logger.info("%.0f초 후 재연결", _backoff)
            time.sleep(_backoff)
            _backoff = min(_backoff * 2, 60.0)
