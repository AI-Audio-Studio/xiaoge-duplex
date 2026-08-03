
# 小歌 -> 阿木同学
OLD = "小歌"
NEW = "阿木同学"
BASE = "/data/home/allen.wangmh/software/xiaoge/xiaoge-duplex-main_bak708/examples/voice_agents"


def patch(path):
    text = open(path, encoding="utf-8").read()
    orig = text
    text = text.replace(OLD, NEW)
    if text == orig:
        print("no change: " + path.split("/")[-1])
    else:
        open(path, "w", encoding="utf-8").write(text)
        print("patched:   " + path.split("/")[-1])


patch(BASE + "/web_ui_agent.py")
patch(BASE + "/app/session_state.py")
patch(BASE + "/listening_mode.py")
patch(BASE + "/app/listening_host.py")
patch(BASE + "/app/setup_taps.py")
patch(BASE + "/app/online_interrupt_host.py")
print("done")
