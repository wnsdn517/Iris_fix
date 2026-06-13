from iris import IrisClient

bot = IrisClient(host="localhost", port=3000)


@bot.on('message')
async def on_message(e):
    print(f"[메시지] {e['room']} / {e['sender']}: {e['msg']}")

    if e['msg'] == '핑':
        room = e['raw']['chat_id']
        await bot.send_message(room, '퐁')


@bot.on('write')
async def on_write(e):
    # 봇이 보낸 메시지가 DB에 기록되면 여기서 확인됨
    print(f"[전송확인] → {e['room']}: {e['msg']}")


@bot.on('join')
async def on_join(e):
    print(f"[입장] {e['room']} ← {e['sender']}")


@bot.on('leave')
async def on_leave(e):
    print(f"[퇴장] {e['room']} → {e['sender']}")


if __name__ == '__main__':
    bot.run()
