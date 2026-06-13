import asyncio
import json
import httpx
import websockets
from typing import Callable, Dict, List, Any, Optional


class IrisClient:
    """
    Iris WebSocket + HTTP 클라이언트.

    사용 예:
        bot = IrisClient(host="localhost", port=3000)

        @bot.on('message')
        async def on_msg(e):
            if e['msg'] == '안녕':
                await bot.send_message(e['raw']['chat_id'], '안녕!')

        @bot.on('write')
        async def on_write(e):
            print(f"[확인] 전송됨 → {e['room']}: {e['msg']}")

        bot.run()

    이벤트 종류:
        'message' : 일반 텍스트 (type=1), 본인 제외
        'write'   : 봇이 보낸 메시지가 DB에 기록됨 (전송 확인)
        'join'    : 입장 (type=2)
        'leave'   : 퇴장 (type=3)
        'event'   : 모든 이벤트 (위 모두 포함)
    """

    def __init__(self, host: str = "localhost", port: int = 3000):
        self.base_url = f"http://{host}:{port}"
        self.ws_url = f"ws://{host}:{port}/ws"
        self._handlers: Dict[str, List[Callable]] = {}
        self._bot_id: Optional[int] = None
        self._running = False

    # ──────────────────────────────────────────
    # 이벤트 등록
    # ──────────────────────────────────────────

    def on(self, event: str):
        """이벤트 핸들러 데코레이터."""
        def decorator(func: Callable):
            self._handlers.setdefault(event, []).append(func)
            return func
        return decorator

    async def _dispatch(self, event: str, data: dict):
        for handler in self._handlers.get(event, []):
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(data)
                else:
                    handler(data)
            except Exception as e:
                print(f"[Iris] '{event}' 핸들러 오류: {e}")
        for handler in self._handlers.get('event', []):
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(data)
                else:
                    handler(data)
            except Exception as e:
                print(f"[Iris] 'event' 핸들러 오류: {e}")

    # ──────────────────────────────────────────
    # WebSocket 수신
    # ──────────────────────────────────────────

    async def _listen(self):
        async with websockets.connect(self.ws_url) as ws:
            print(f"[Iris] WebSocket 연결됨: {self.ws_url}")
            async for raw in ws:
                try:
                    data = json.loads(raw)
                    raw_json = data.get('json', {})
                    if isinstance(raw_json, str):
                        raw_json = json.loads(raw_json)

                    user_id = _int(raw_json.get('user_id'))
                    msg_type = str(raw_json.get('type', '1'))

                    # v 필드에서 isMine 추출 (백업)
                    v = raw_json.get('v', {})
                    if isinstance(v, str):
                        try:
                            v = json.loads(v)
                        except Exception:
                            v = {}
                    is_mine = v.get('isMine', False) or (
                        self._bot_id is not None and user_id == self._bot_id
                    )

                    event_data = {
                        'msg': data.get('msg', ''),
                        'room': data.get('room', ''),
                        'sender': data.get('sender', ''),
                        'user_id': user_id,
                        'type': msg_type,
                        'is_mine': is_mine,
                        'raw': raw_json,
                    }

                    if is_mine:
                        await self._dispatch('write', event_data)
                    elif msg_type == '2':
                        await self._dispatch('join', event_data)
                    elif msg_type == '3':
                        await self._dispatch('leave', event_data)
                    else:
                        await self._dispatch('message', event_data)

                except Exception as e:
                    print(f"[Iris] 이벤트 파싱 오류: {e}")

    async def start(self):
        self._bot_id = await self._fetch_bot_id()
        print(f"[Iris] bot_id={self._bot_id}")
        self._running = True
        while self._running:
            try:
                await self._listen()
            except Exception as e:
                print(f"[Iris] WebSocket 끊김: {e} — 3초 후 재연결...")
                await asyncio.sleep(3)

    def run(self):
        """동기 진입점."""
        asyncio.run(self.start())

    # ──────────────────────────────────────────
    # HTTP 전송 메서드
    # ──────────────────────────────────────────

    async def send_message(self, room: int, msg: str, thread_id: int = None) -> dict:
        body = {"room": str(room), "type": "text", "data": msg}
        if thread_id is not None:
            body["threadId"] = str(thread_id)
        return await self._post("/reply", body)

    async def send_audio(self, room: int, file_path: str) -> dict:
        return await self._post("/reply", {"room": str(room), "type": "audio", "data": file_path})

    async def send_file(self, room: int, file_path: str) -> dict:
        return await self._post("/reply", {"room": str(room), "type": "file", "data": file_path})

    async def send_image(self, room: int, base64_data: str) -> dict:
        return await self._post("/reply", {"room": str(room), "type": "image", "data": base64_data})

    async def send_images(self, room: int, base64_list: list) -> dict:
        return await self._post("/reply", {"room": str(room), "type": "image_multiple", "data": base64_list})

    async def exec(self, command: str) -> dict:
        return await self._post("/exec", {"command": command})

    async def query(self, sql: str, bind: list = None) -> list:
        body = {"query": sql}
        if bind:
            body["bind"] = bind
        resp = await self._post("/query", body)
        return resp.get("data", [])

    # ──────────────────────────────────────────
    # 내부 유틸
    # ──────────────────────────────────────────

    async def _post(self, path: str, body: dict) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{self.base_url}{path}", json=body)
            return resp.json()

    async def _fetch_bot_id(self) -> Optional[int]:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{self.base_url}/config")
                return _int(resp.json().get("bot_id"))
        except Exception as e:
            print(f"[Iris] bot_id 가져오기 실패: {e}")
            return None


def _int(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
