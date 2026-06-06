from __future__ import annotations

import base64
import concurrent.futures
import gc
import json
import logging
import sys
import threading
import time
import traceback
import typing as t
from dataclasses import dataclass
from io import BufferedIOBase, BytesIO

import requests
from PIL import Image
from requests.adapters import HTTPAdapter

from modules.transmission import begin_send, complete_send, mark_write_event, record_miss
from modules.utils import transmission_screen_guard

log_api     = logging.getLogger("iris.api")
log_emitter = logging.getLogger("iris.emitter")


# ── IrisRequest ──────────────────────────────────────────────────────────────

@dataclass
class IrisRequest:
    msg: str
    room: str
    sender: str
    raw: dict
    is_lite: bool = False
    is_group_chat: bool | None = None
    profile_image: str = None


# ── IrisAPI ───────────────────────────────────────────────────────────────────

class IrisAPI:
    _CONFIG_PATH = "./config.json"

    _REQUEST_TIMEOUT = 15

    def __init__(self, iris_endpoint: str):
        self.iris_endpoint = iris_endpoint
        self._session = requests.Session()
        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=16)
        self._session.mount("http://", adapter)
        self._session.mount("https://", adapter)
        self.allowed_rooms = self._load_allowed_rooms()
        log_api.debug("IrisAPI 초기화 완료: %s", iris_endpoint)

    # ── whitelist ─────────────────────────────────────────────────────────────

    def _load_allowed_rooms(self) -> set[str]:
        try:
            with open(self._CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            rooms = {str(r) for r in data.get("allowed_rooms", [])}
            if rooms:
                log_api.info("화이트리스트 로드: %d개 방", len(rooms))
            else:
                log_api.debug("화이트리스트 미설정 — 모든 방 허용")
            return rooms
        except FileNotFoundError:
            log_api.debug("config.json 없음 — 화이트리스트 비활성")
            return set()
        except Exception as e:
            log_api.warning("화이트리스트 로드 실패: %s", e)
            return set()

    def _is_allowed(self, room_id) -> bool:
        # 방 접근 제어는 utils/rooms.py 에서 처리 — API 레이어에서는 차단하지 않음
        return True

    def add_whitelist(self, room_id) -> bool:
        try:
            try:
                with open(self._CONFIG_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except FileNotFoundError:
                data = {}
            rooms = data.get("allowed_rooms", [])
            if str(room_id) not in [str(r) for r in rooms]:
                rooms.append(str(room_id))
                data["allowed_rooms"] = rooms
                with open(self._CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            self.allowed_rooms.add(str(room_id))
            log_api.info("화이트리스트 추가: %s", room_id)
            return True
        except Exception as e:
            log_api.error("화이트리스트 추가 실패: %s", e)
            return False

    def del_whitelist(self, room_id) -> bool:
        try:
            try:
                with open(self._CONFIG_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except FileNotFoundError:
                data = {}
            rooms = data.get("allowed_rooms", [])
            data["allowed_rooms"] = [r for r in rooms if str(r) != str(room_id)]
            with open(self._CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self.allowed_rooms.discard(str(room_id))
            log_api.info("화이트리스트 제거: %s", room_id)
            return True
        except Exception as e:
            log_api.error("화이트리스트 제거 실패: %s", e)
            return False

    # ── 내부 ──────────────────────────────────────────────────────────────────

    def __parse(self, res: requests.Response) -> dict:
        log_api.debug("응답 %d  %s", res.status_code, res.url)
        try:
            data: dict = res.json()
        except Exception:
            raise Exception(f"Iris 응답 JSON 파싱 오류: {res.text}")
        if not 200 <= res.status_code <= 299:
            raise Exception(f"Iris 오류: {data.get('message', '알 수 없는 오류')}")
        return data

    # ── 전송 API ──────────────────────────────────────────────────────────────

    def reply(self, room_id: int, msg: str, thread_id: int | None = None, attachment: str | None = None):
        if not self._is_allowed(room_id):
            return
        log_api.debug("reply → room=%s thread=%s msg=%.80r", room_id, thread_id, msg)

        # md attachment 인데 ADB 끊김 → attachment 제거, md 마크업 제거 후 텍스트만 전송
        is_markdown = False
        if attachment is not None:
            try:
                att_obj = json.loads(attachment)
                if isinstance(att_obj, dict) and att_obj.get("markdown"):
                    is_markdown = True
            except Exception:
                pass

        if is_markdown and not transmission_screen_guard.adb_ok():
            log_api.warning("reply_markdown: ADB 끊김 — markdown 제거 후 텍스트 전송 (room=%s)", room_id)
            import re as _re
            plain = _re.sub(r"\*\*(.+?)\*\*", r"\1", msg)   # bold
            plain = _re.sub(r"\*(.+?)\*",     r"\1", plain)  # italic
            plain = _re.sub(r"~~(.+?)~~",     r"\1", plain)  # strike
            plain = _re.sub(r"`(.+?)`",       r"\1", plain)  # inline code
            plain = _re.sub(r"^#{1,6}\s+",    "",    plain, flags=_re.MULTILINE)  # heading
            attachment = None
            msg = plain

        token = begin_send("text", room_id, detail=f"thread={thread_id}")
        t0 = time.monotonic()

        def _do_send():
            payload = {"type": "text", "room": str(room_id), "data": str(msg)}
            if thread_id is not None:
                payload["threadId"] = str(thread_id)
            if attachment is not None:
                payload["attachment"] = attachment
            return self.__parse(self._session.post(f"{self.iris_endpoint}/reply", json=payload, timeout=self._REQUEST_TIMEOUT))

        try:
            if attachment is not None:
                with transmission_screen_guard("reply_markdown", restore_delay_sec=8.0):
                    result = _do_send()
            else:
                result = _do_send()
            complete_send(token, ok=True, elapsed_ms=round((time.monotonic() - t0) * 1000, 1))
            return result
        except Exception as e:
            complete_send(token, ok=False, elapsed_ms=round((time.monotonic() - t0) * 1000, 1), error=str(e))
            record_miss("text", room_id, reason=str(e), token=token)
            raise

    def reply_file(self, room_id: int, file_path: str, attachment: str | None = None):
        if not self._is_allowed(room_id):
            return
        import os
        import shutil
        import threading

        abs_src = os.path.abspath(file_path)
        log_api.debug("reply_file → room=%s path=%s attachment=%s", room_id, abs_src, attachment)

        if not transmission_screen_guard.adb_ok():
            log_api.warning("reply_file: ADB 끊김 — 전송 누락 (room=%s path=%s)", room_id, abs_src)
            token = begin_send("file", room_id, detail=abs_src)
            record_miss("file", room_id, reason="adb disconnected", token=token)
            try:
                self.__parse(self._session.post(
                    f"{self.iris_endpoint}/reply",
                    json={"type": "text", "room": str(room_id),
                          "data": "⚠️ ADB 연결 끊김으로 파일 전송이 누락될 수 있습니다."},
                ))
            except Exception:
                pass
            return

        token = begin_send("file", room_id, detail=abs_src)
        t0 = time.monotonic()

        # iris 앱은 Android 샌드박스 때문에 Termux 내부 경로를 읽을 수 없음.
        # /sdcard/ (외부저장소)로 복사해 양쪽 모두 접근 가능한 경로를 전달한다.
        shared_dir = "/sdcard/iris_tmp"
        try:
            os.makedirs(shared_dir, exist_ok=True)
            dest = os.path.join(shared_dir, os.path.basename(abs_src))
            shutil.copy2(abs_src, dest)
            send_path = dest
        except Exception as e:
            log_api.warning("sdcard 복사 실패, 원본 경로 사용: %s", e)
            send_path = abs_src

        def _cleanup():
            import time as _t
            _t.sleep(60)
            try:
                if send_path != abs_src:
                    os.unlink(send_path)
            except Exception:
                pass

        threading.Thread(target=_cleanup, daemon=True).start()

        payload: dict = {"type": "file", "room": str(room_id), "data": send_path}
        if attachment is not None:
            payload["attachment"] = attachment

        try:
            with transmission_screen_guard("reply_file", restore_delay_sec=8.0):
                result = self.__parse(
                    self._session.post(
                        f"{self.iris_endpoint}/reply",
                        json=payload,
                        timeout=self._REQUEST_TIMEOUT,
                    )
                )
            complete_send(token, ok=True, elapsed_ms=round((time.monotonic() - t0) * 1000, 1))
            return result
        except Exception as e:
            complete_send(token, ok=False, elapsed_ms=round((time.monotonic() - t0) * 1000, 1), error=str(e))
            record_miss("file", room_id, reason=str(e), token=token)
            raise

    def reply_media(
        self,
        room_id: int,
        files: t.List[BufferedIOBase | bytes | Image.Image | str],
        thread_id: int | None = None,
    ):
        if not self._is_allowed(room_id):
            return
        if not isinstance(files, list):
            files = [files]

        log_api.debug("reply_media → room=%s files=%d개", room_id, len(files))

        if not transmission_screen_guard.adb_ok():
            log_api.warning("reply_media: ADB 끊김 — 이미지 전송 누락 (room=%s)", room_id)
            token = begin_send("media", room_id, detail=f"files={len(files)}")
            record_miss("media", room_id, reason="adb disconnected", token=token)
            try:
                self.__parse(self._session.post(
                    f"{self.iris_endpoint}/reply",
                    json={"type": "text", "room": str(room_id),
                          "data": "⚠️ ADB 연결 끊김으로 이미지 전송이 누락될 수 있습니다."},
                ))
            except Exception:
                pass
            return

        token = begin_send("media", room_id, detail=f"files={len(files)}")
        t0 = time.monotonic()
        data = []
        for file in files:
            try:
                encoded = self._encode_file(file)
                if encoded:
                    data.append(encoded)
            except Exception as e:
                log_api.error("이미지 인코딩 실패: %s", e)

        if not data:
            log_api.warning("전송 가능한 이미지가 없습니다 (room=%s)", room_id)
            record_miss("media", room_id, reason="no encodable files", token=token)
            return

        try:
            with transmission_screen_guard("reply_media", restore_delay_sec=8.0):
                payload = {"type": "image_multiple", "room": str(room_id), "data": data}
                if thread_id is not None:
                    payload["threadId"] = str(thread_id)
                result = self.__parse(self._session.post(f"{self.iris_endpoint}/reply", json=payload, timeout=self._REQUEST_TIMEOUT))
            complete_send(token, ok=True, elapsed_ms=round((time.monotonic() - t0) * 1000, 1))
            return result
        except Exception as e:
            complete_send(token, ok=False, elapsed_ms=round((time.monotonic() - t0) * 1000, 1), error=str(e))
            record_miss("media", room_id, reason=str(e), token=token)
            raise

    def _encode_file(self, file) -> str | None:
        if isinstance(file, BufferedIOBase):
            log_api.debug("인코딩: BufferedIOBase")
            return base64.b64encode(file.read()).decode()
        if isinstance(file, bytes):
            log_api.debug("인코딩: bytes (%d B)", len(file))
            return base64.b64encode(file).decode()
        if isinstance(file, Image.Image):
            log_api.debug("인코딩: PIL Image %s", file.size)
            buf = BytesIO()
            file.convert("RGBA").save(buf, format="PNG")
            return base64.b64encode(buf.getvalue()).decode()
        if isinstance(file, str):
            if file.startswith("http"):
                log_api.debug("이미지 다운로드: %s", file)
                res = self._session.get(file)
                if res.status_code != 200:
                    log_api.warning("이미지 다운로드 실패: %s (HTTP %d)", file, res.status_code)
                    return None
                return base64.b64encode(res.content).decode()
            log_api.debug("파일 읽기: %s", file)
            with open(file, "rb") as f:
                return base64.b64encode(f.read()).decode()
        log_api.warning("지원하지 않는 파일 형식: %s", type(file))
        return None

    # ── 기타 API ──────────────────────────────────────────────────────────────

    def decrypt(self, enc: int, b64_ciphertext: str, user_id: int) -> str | None:
        log_api.debug("decrypt → enc=%d user_id=%s", enc, user_id)
        try:
            res = self._session.post(
                f"{self.iris_endpoint}/decrypt",
                json={"enc": enc, "b64_ciphertext": b64_ciphertext, "user_id": user_id},
                timeout=10,
            )
            return self.__parse(res).get("plain_text")
        except Exception as e:
            log_api.debug("decrypt 실패 (enc=%d user_id=%s): %s", enc, user_id, e)
            return None

    def query(self, query: str, bind: list[t.Any] | None = None) -> list[dict]:
        log_api.debug("query → %s | bind=%s", query.split()[0:4], bind)
        res = self._session.post(
            f"{self.iris_endpoint}/query",
            json={"query": query, "bind": bind or []},
            timeout=self._REQUEST_TIMEOUT,
        )
        result = self.__parse(res).get("data", [])
        log_api.debug("query ← %d rows", len(result))
        return result

    def get_info(self):
        log_api.debug("get_info 요청")
        return self.__parse(self._session.get(f"{self.iris_endpoint}/config", timeout=self._REQUEST_TIMEOUT))

    def get_aot(self):
        log_api.debug("get_aot 요청")
        return self.__parse(self._session.get(f"{self.iris_endpoint}/aot", timeout=self._REQUEST_TIMEOUT))

    def exec_command(self, command: str) -> dict:
        """POST /exec — 쉘 명령어 실행.

        Returns:
            {"stdout": str, "exitCode": int}
            exitCode == -1 이면 30초 타임아웃으로 강제 종료된 것.

        Example:
            result = api.exec_command("ls /sdcard/iris_tmp")
            print(result["stdout"])
        """
        log_api.debug("exec_command → %r", command)
        res = self._session.post(
            f"{self.iris_endpoint}/exec",
            json={"command": command},
            timeout=35,  # 서버 타임아웃 30초 + 여유 5초
        )
        return self.__parse(res)

    def get_stats(
        self,
        room_id: int | str | None = None,
        user_id: int | str | None = None,
    ) -> dict:
        """GET /stats — 통계 조회.

        파라미터 조합에 따라 다른 데이터를 반환:
          - 없음:              roomStats (방별 메시지 수) + hourlyUsage (봇 시간대별 활동)
          - room_id:           userStats (해당 방 유저별 통계) + hourlyUsage
          - user_id:           hourlyUsage (해당 유저 시간대별 활동)
          - room_id + user_id: userStats (해당 방에서 해당 유저의 시간대별 활동)

        Returns:
            {
                "roomStats":    [{"chat_id", "msg_count", "last_activity"}, ...] | None,
                "hourlyUsage":  [{"hour", "count"}, ...] | None,
                "userStats":    [{"user_id", "msg_count", "first_at", "last_at"}, ...] | None,
            }

        Example:
            stats = api.get_stats()
            for room in stats["roomStats"]:
                print(room["chat_id"], room["msg_count"])

            room_stats = api.get_stats(room_id=12345678)
            for user in room_stats["userStats"]:
                print(user["user_id"], user["msg_count"])
        """
        params: dict[str, str] = {}
        if room_id is not None:
            params["roomId"] = str(room_id)
        if user_id is not None:
            params["userId"] = str(user_id)
        log_api.debug("get_stats → params=%s", params)
        res = self._session.get(
            f"{self.iris_endpoint}/stats",
            params=params,
            timeout=self._REQUEST_TIMEOUT,
        )
        return self.__parse(res)


# ── EventEmitter ──────────────────────────────────────────────────────────────

_MAX_WORKERS  = 32
_GC_INTERVAL  = 60
_WARN_THREADS = 60


class EventEmitter:
    def __init__(self, max_workers: int = _MAX_WORKERS):
        self.ev: dict[str, list[t.Callable]] = {}
        self.pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="iris-ev",
        )
        self._pending: list[concurrent.futures.Future] = []
        self._pending_lock = threading.Lock()
        self._lock = threading.Lock()
        self._last_gc = time.monotonic()
        log_emitter.debug("EventEmitter 초기화: max_workers=%d", max_workers)

    def register(self, name: str, func: t.Callable) -> None:
        name = name.lower()
        with self._lock:
            self.ev.setdefault(name, []).append(func)
        log_emitter.debug("핸들러 등록: event=%s func=%s", name, func.__name__)

    def unregister(self, name: str, func: t.Callable) -> bool:
        name = name.lower()
        with self._lock:
            handlers = self.ev.get(name)
            if not handlers:
                return False
            for idx, handler in enumerate(handlers):
                if handler is func:
                    handlers.pop(idx)
                    if not handlers:
                        self.ev.pop(name, None)
                    log_emitter.debug("핸들러 해제: event=%s func=%s", name, func.__name__)
                    return True
        return False

    def emit(self, name: str, args: list[t.Any]) -> None:
        name = name.lower()
        with self._lock:
            handlers = list(self.ev.get(name, []))

        log_emitter.debug("emit '%s' → %d 핸들러", name, len(handlers))

        for func in handlers:
            future = self.pool.submit(self._handle_event, func, name, args)
            with self._pending_lock:
                self._pending.append(future)

        now = time.monotonic()
        if now - self._last_gc >= _GC_INTERVAL:
            self._gc()
            self._last_gc = now

    def _handle_event(self, func: t.Callable, name: str, args: list[t.Any]) -> None:
        log_emitter.debug("실행: event=%s func=%s", name, func.__name__)
        try:
            func(*args)
            log_emitter.debug("완료: event=%s func=%s", name, func.__name__)
        except Exception as e:
            if name == "error":
                log_emitter.error("error 핸들러에서 예외 발생 (%s): %s", func.__name__, e)
                traceback.print_exc()
                return
            log_emitter.error("'%s' 이벤트 핸들러 예외 (%s): %s", name, func.__name__, e)
            traceback.print_exc()
            try:
                from iris.bot.models import ErrorContext
                self.emit("error", [ErrorContext(event=name, func=func, exception=e, args=args)])
            except Exception:
                pass
        finally:
            sys.stdout.flush()

    def _gc(self) -> None:
        with self._pending_lock:
            before = len(self._pending)
            self._pending = [f for f in self._pending if not f.done()]
            removed = before - len(self._pending)
        if removed:
            gc.collect()
            log_emitter.debug("GC: future %d개 정리", removed)
        active = threading.active_count()
        if active > _WARN_THREADS:
            log_emitter.warning("활성 스레드 %d개 (pending: %d)", active, len(self._pending))

    def shutdown(self, wait: bool = True) -> None:
        log_emitter.info("EventEmitter 종료 (wait=%s)", wait)
        self._gc()
        self.pool.shutdown(wait=wait, cancel_futures=True)
