import abc
import asyncio
import base64
import json
import logging
import typing as t
from uuid import uuid4
from urllib.parse import quote

import httpx
import requests

from iris.pykv import PyKV

logger = logging.getLogger("KakaoLink")

KAKAOTALK_VERSION = "25.2.1"
ANDROID_SDK_VER = 33
ANDROID_WEBVIEW_UA = (
    "Mozilla/5.0 (Linux; Android 13; SM-G998B Build/TP1A.220624.014; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/114.0.5735.60 Mobile Safari/537.36"
)


# ── 예외 ──────────────────────────────────────────────────────────────────────

class KakaoLinkException(Exception):
    pass

class KakaoLinkReceiverNotFoundExcepetion(KakaoLinkException):
    pass

class KakaoLinkLoginExcepetion(KakaoLinkException):
    pass

class KakaoLink2FAExcepetion(KakaoLinkException):
    pass

class KakaoLinkSendExcepetion(KakaoLinkException):
    pass


# ── 내부 헬퍼 ─────────────────────────────────────────────────────────────────

class _CookieStorage:
    def __init__(self):
        self._store: dict = {}

    async def save(self, cookies: dict):
        self._store = cookies

    async def load(self) -> dict:
        return self._store

    def clear(self):
        self._store = {}


class _AuthorizationProvider:
    def __init__(self, iris_url: str):
        self._url = f"http://{iris_url}"

    async def get_authorization(self) -> str:
        aot = requests.get(f"{self._url}/aot").json()["aot"]
        return f"{aot['access_token']}-{aot['d_id']}"


# ── KakaoLink ─────────────────────────────────────────────────────────────────

class KakaoLink:
    def __init__(
        self,
        iris_url: str,
        default_app_key: str | None = None,
        default_origin: str | None = None,
    ):
        self.default_app_key = default_app_key
        self.default_origin = default_origin
        self._cookies: dict = {}
        self._send_lock = asyncio.Lock()
        self._auth_provider = _AuthorizationProvider(iris_url)
        self._cookie_storage = _CookieStorage()

    async def send(
        self,
        receiver_name: str,
        template_id: int,
        template_args: dict,
        app_key: str | None = None,
        origin: str | None = None,
        search_exact: bool = True,
        search_from: t.Literal["ALL", "FRIENDS", "CHATROOMS"] = "ALL",
        search_room_type: t.Literal["ALL", "OpenMultiChat", "MultiChat", "DirectChat"] = "ALL",
    ):
        app_key = app_key or self.default_app_key
        origin = origin or self.default_origin
        if not app_key or not origin:
            raise KakaoLinkException("app_key 또는 origin은 비어있을 수 없습니다")

        ka = self._get_ka(origin)
        async with self._send_lock:
            async with httpx.AsyncClient(cookies=self._cookies) as client:
                picker_data = await self._get_picker_data(client, app_key, ka, template_id, template_args)
                try:
                    checksum  = picker_data["checksum"]
                    csrf      = picker_data["csrfToken"]
                    short_key = picker_data["shortKey"]
                except KeyError:
                    raise KakaoLinkSendExcepetion()

                receiver = self._picker_data_search(
                    receiver_name, picker_data, search_exact, search_from, search_room_type
                )
                await self._picker_send(client, app_key, short_key, checksum, csrf, receiver)

    async def _picker_send(
        self,
        client: httpx.AsyncClient,
        app_key: str,
        short_key: str,
        checksum: str,
        csrf: str,
        receiver: dict,
    ):
        res = await client.post(
            "https://sharer.kakao.com/picker/send",
            data={
                "app_key": app_key,
                "short_key": short_key,
                "checksum": checksum,
                "_csrf": csrf,
                "receiver": base64.urlsafe_b64encode(
                    json.dumps(receiver, ensure_ascii=False).encode()
                ).decode(),
            },
        )
        if res.status_code == 400:
            raise KakaoLinkSendExcepetion()

    def _picker_data_search(
        self,
        receiver_name: str,
        picker_data: dict,
        search_exact: bool,
        search_from: str,
        search_room_type: str,
    ) -> dict:
        candidates = []
        if search_from in ("ALL", "CHATROOMS"):
            candidates += picker_data.get("chats", [])
        if search_from in ("ALL", "FRIENDS"):
            candidates += picker_data.get("friends", [])

        for receiver in candidates:
            chat_type = receiver.get("chat_room_type")
            if chat_type and search_room_type != "ALL" and search_room_type != chat_type:
                continue
            title = receiver.get("title") or receiver.get("profile_nickname", "")
            if search_exact and title == receiver_name:
                return receiver
            if not search_exact and receiver_name in title:
                return receiver

        raise KakaoLinkReceiverNotFoundExcepetion()

    async def _get_picker_data(
        self,
        client: httpx.AsyncClient,
        app_key: str,
        ka: str,
        template_id: int,
        template_args: dict,
    ) -> dict:
        res = await client.post(
            "https://sharer.kakao.com/picker/link",
            headers=self._get_web_headers(),
            data={
                "app_key": app_key,
                "ka": ka,
                "validation_action": "custom",
                "validation_params": json.dumps(
                    {"link_ver": "4.0", "template_id": template_id, "template_args": template_args},
                    ensure_ascii=False,
                ),
            },
            follow_redirects=True,
        )

        if res.url.path.startswith("/login"):
            continue_url = res.url.params.get("continue")
            await self._login(client)
            res = await client.get(continue_url, headers=self._get_web_headers(), follow_redirects=True)

        if res.url.path.startswith("/talk_tms_auth/service"):
            logger.info("카카오링크 전송: 추가인증 해결 중")
            continue_url = await self._solve_two_factor_auth(client, res.text)
            res = await client.get(continue_url, headers=self._get_web_headers(), follow_redirects=True)

        return json.loads(
            base64.urlsafe_b64decode(
                res.text.split('window.serverData = "')[1].split('"')[0].strip() + "===="
            )
        )["data"]

    async def init(self):
        self._cookies = await self._cookie_storage.load()
        async with httpx.AsyncClient(cookies=self._cookies) as client:
            await self._login(client)

    async def _login(self, client: httpx.AsyncClient):
        authorization = await self._auth_provider.get_authorization()
        self._cookies = {}
        self._cookie_storage.clear()
        client.cookies.clear()

        if await self._check_authorized(client):
            return

        tgt_token = await self._get_tgt_token(client, authorization)
        await self._submit_tgt_token(client, tgt_token)

        if not await self._check_authorized(client):
            logger.error("카카오링크 로그인: 알 수 없는 이유로 로그인이 되지 않았습니다")

        self._cookies = dict(client.cookies)
        await self._cookie_storage.save(self._cookies)

    async def _solve_two_factor_auth(self, client: httpx.AsyncClient, tfa_html: str) -> str:
        try:
            props = json.loads(
                tfa_html.split('<script id="__NEXT_DATA__" type="application/json">')[1]
                .split("</script>")[0]
                .strip()
            )
            context        = props["props"]["pageProps"]["pageContext"]["context"]
            common_context = props["props"]["pageProps"]["pageContext"]["commonContext"]
            token          = context["token"]
            continueUrl    = context["continueUrl"]
            csrf           = common_context["_csrf"]
        except Exception:
            raise KakaoLink2FAExcepetion()

        await self._confirm_token(client, token)

        res = await client.post(
            "https://accounts.kakao.com/api/v2/talk_tms_auth/poll_from_service.json",
            headers=self._get_web_headers(),
            json={"_csrf": csrf, "token": token},
        )
        if res.json().get("status") != 0:
            raise KakaoLink2FAExcepetion()

        return continueUrl

    async def _confirm_token(self, client: httpx.AsyncClient, two_factor_token: str):
        res = await client.get(
            "https://auth.kakao.com/fa/main.html",
            params={
                "os": "android",
                "country_iso": "KR",
                "lang": "ko",
                "v": KAKAOTALK_VERSION,
                "os_version": ANDROID_SDK_VER,
                "page": "additional_auth_with_token",
                "additional_auth_token": two_factor_token,
                "close_on_completion": "true",
                "talk_tms_auth_type": "from_service",
            },
        )
        try:
            csrf = res.text.split('<meta name="csrf-token" content="')[1].split('"')[0].strip()
            data = json.loads(
                res.text.split("var options =")[1].split("new PageBuilder()")[0].strip("; \t\n")
            )
        except Exception:
            raise KakaoLink2FAExcepetion()

        res = await client.post(
            "https://auth.kakao.com/talk_tms_auth/confirm_token.json",
            data={
                "client_id": data["client_id"],
                "lang": "ko",
                "os": "android",
                "v": KAKAOTALK_VERSION,
                "webview_v": "2",
                "token": data["additionalAuthToken"],
                "talk_tms_auth_type": "from_service",
                "authenticity_token": csrf,
            },
        )
        if res.json().get("status") != 0:
            raise KakaoLink2FAExcepetion()

    async def _check_authorized(self, client: httpx.AsyncClient) -> bool:
        res = await client.get(
            "https://e.kakao.com/api/v1/users/me",
            headers={**self._get_web_headers(), "referer": "https://e.kakao.com/"},
        )
        return res.json().get("result", {}).get("status") == "VALID"

    async def _submit_tgt_token(self, client: httpx.AsyncClient, tgt_token: str):
        res = await client.get(
            "https://e.kakao.com",
            headers={**self._get_web_headers(), "ka-tgt": tgt_token},
        )
        res.raise_for_status()

    async def _get_tgt_token(self, client: httpx.AsyncClient, token: str) -> str:
        res = await client.post(
            "https://api-account.kakao.com/v1/auth/tgt",
            headers=self._get_app_headers(token),
            data={"key_type": "talk_session_info", "key": token, "referer": "talk"},
        )
        res_json = res.json()
        if res_json.get("code") != 0:
            raise KakaoLinkLoginExcepetion()
        return res_json["token"]

    def _get_ka(self, origin: str) -> str:
        return f"sdk/1.43.5 os/javascript sdk_type/javascript lang/ko-KR device/Linux armv7l origin/{quote(origin)}"

    def _get_app_headers(self, token: str) -> dict:
        return {
            "A": f"android/{KAKAOTALK_VERSION}/ko",
            "C": str(uuid4()),
            "User-Agent": f"KT/{KAKAOTALK_VERSION} An/13 ko",
            "Authorization": token,
        }

    def _get_web_headers(self) -> dict:
        return {
            "User-Agent": f"{ANDROID_WEBVIEW_UA} KAKAOTALK/{KAKAOTALK_VERSION} (INAPP)",
            "X-Requested-With": "com.kakao.talk",
        }


# ── IrisLink (고수준 래퍼) ────────────────────────────────────────────────────

class IrisLink:
    def __init__(self, iris_url: str):
        try:
            kv = PyKV()
            self.iris_url = iris_url
            config = kv.get("kakaolink_config")
            if not isinstance(config, dict) or "app_key" not in config or "origin" not in config:
                raise ValueError(
                    "KakaoLink 설정이 없습니다. iris kakaolink <app_key> <origin> 명령어로 설정하세요."
                )
            self.client = KakaoLink(
                iris_url=iris_url,
                default_app_key=config["app_key"],
                default_origin=config["origin"],
            )
            asyncio.run(self.client.init())
        except Exception as e:
            print(f"IrisLink 초기화 오류: {e}")

    def send(
        self,
        receiver_name: str,
        template_id: int,
        template_args: dict,
        app_key: str | None = None,
        origin: str | None = None,
        search_exact: bool = True,
        search_from: t.Literal["ALL", "FRIENDS", "CHATROOMS"] = "ALL",
        search_room_type: t.Literal["ALL", "OpenMultiChat", "MultiChat", "DirectChat"] = "ALL",
    ):
        asyncio.run(
            self.client.send(
                receiver_name=receiver_name,
                template_id=template_id,
                template_args=template_args,
                app_key=app_key,
                origin=origin,
                search_exact=search_exact,
                search_from=search_from,
                search_room_type=search_room_type,
            )
        )

    def __repr__(self) -> str:
        return f"<IrisLink(iris_url={self.iris_url})>"
