"""
이미지 전송 테스트 스크립트.
on('write') 이벤트로 전송 확인.

사용법:
    python test_image.py <room_id> <image_path> [image_path2 ...]

예시:
    python test_image.py 1234567890 /sdcard/test.png
    python test_image.py 1234567890 /sdcard/a.png /sdcard/b.png
"""
import asyncio
import base64
import sys
import time
from iris import IrisClient

HOST = "localhost"
PORT = 3000
CONFIRM_TIMEOUT = 30  # 초


async def main():
    if len(sys.argv) < 3:
        print("사용법: python test_image.py <room_id> <image_path> [image_path2 ...]")
        sys.exit(1)

    room_id = int(sys.argv[1])
    image_paths = sys.argv[2:]

    # base64 인코딩
    images_b64 = []
    for path in image_paths:
        with open(path, 'rb') as f:
            images_b64.append(base64.b64encode(f.read()).decode())
        print(f"[테스트] 이미지 로드: {path} ({len(images_b64[-1])} bytes b64)")

    bot = IrisClient(host=HOST, port=PORT)
    confirmed = asyncio.Event()
    send_time = None

    @bot.on('write')
    async def on_write(e):
        if confirmed.is_set():
            return
        elapsed = round(time.time() - send_time, 2) if send_time else '?'
        print(f"[전송확인] ✓ {elapsed}s 후 DB 기록됨 → room={e['room']} msg={repr(e['msg'])}")
        confirmed.set()

    # 백그라운드에서 WS 수신 시작
    ws_task = asyncio.create_task(bot.start())
    await asyncio.sleep(1)  # WS 연결 대기

    # 전송
    send_time = time.time()
    if len(images_b64) == 1:
        print(f"[테스트] send_image → room={room_id}")
        resp = await bot.send_image(room_id, images_b64[0])
    else:
        print(f"[테스트] send_images ({len(images_b64)}장) → room={room_id}")
        resp = await bot.send_images(room_id, images_b64)

    print(f"[테스트] HTTP 응답: {resp}")

    # write 이벤트 대기
    print(f"[테스트] write 이벤트 대기 중... (최대 {CONFIRM_TIMEOUT}s)")
    try:
        await asyncio.wait_for(confirmed.wait(), timeout=CONFIRM_TIMEOUT)
    except asyncio.TimeoutError:
        print(f"[테스트] ✗ {CONFIRM_TIMEOUT}s 내 전송 확인 없음 (이미지가 bot 명의로 기록 안 됐거나 WS 미수신)")

    ws_task.cancel()


if __name__ == '__main__':
    asyncio.run(main())
