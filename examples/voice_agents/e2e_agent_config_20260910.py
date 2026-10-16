#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""e2e 20260910: 音色+人设面板热配(/api/agentconfig)全链路。

用法: e2e_agent_config.py <BASE> <GW_HOST> <DEVICE>
链路=同一在途会话跨配置变更:POST(网关→任意 READY worker 写共享
data/agent_config.json) → 下一轮 data.text 时会话 worker 按 mtime 热应用
(人设 update_instructions + 音色换 CosyVoice 实例)。
断言:
  T1 你是谁 → 答默认名"小歌"。
  POST 改名"糖糖"+性格+音色 longanwen_v3 → ok;GET 回读一致(名字/音色/目录)。
  T2 你是谁 → 答含"糖糖"不含"小歌";窗口内 AGENT_CONFIG_APPLIED name='糖糖'
     voice=longanwen_v3(轮次钩子实锤,含 TTS 换实例)。
  POST 恢复默认 → T3 你是谁 → 答含"小歌";AGENT_CONFIG_APPLIED name='小歌'。
全程不提接口/API/来源(人设保密)。
"""
import asyncio
import json
import re
import ssl
import sys
import time

import aiohttp
import pymysql

BASE, GW_HOST, DEVICE = sys.argv[1], sys.argv[2], sys.argv[3]
TURN_LOG = f"{BASE}/examples/voice_agents/qwen_voice_turn_metrics.log"

DRAIN_S = 12.0  # 等本句 TTS 播完再动配置/问下一句
SECRET_WORDS = ("接口", "API", "Open", "来源", "数据源")


def fetch_key():
    with open(f"{BASE}/.xg_db_password") as f:
        pw = f.read().strip()
    conn = pymysql.connect(
        host="47.118.58.103", port=3306, user="remote_user",
        password=pw, database="ry-cloud",
        cursorclass=pymysql.cursors.DictCursor, connect_timeout=10)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT api_key FROM sys_api_key "
                        "WHERE status='0' AND api_key LIKE 'sk-%' LIMIT 1")
            row = cur.fetchone()
            if not row:
                raise SystemExit("no active sk- key")
            return row["api_key"]
    finally:
        conn.close()


def parse_log(marker, t_start, t_end):
    out = []
    with open(TURN_LOG, encoding="utf-8", errors="replace") as f:
        for line in f:
            if marker not in line:
                continue
            m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\.(\d{3})", line)
            if not m:
                continue
            ts = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")) \
                + int(m.group(2)) / 1000
            if not (t_start <= ts <= t_end):
                continue
            out.append(line.strip())
    return out


async def main():
    key = fetch_key()
    sslctx = ssl.create_default_context()
    sslctx.check_hostname = False
    sslctx.verify_mode = ssl.CERT_NONE
    conn = aiohttp.TCPConnector(ssl=sslctx)
    fails = []

    def check(label, cond, desc):
        print(f"  [{'PASS' if cond else 'FAIL'}] {label}: {desc}", flush=True)
        if not cond:
            fails.append(f"{label}: {desc}")

    async with aiohttp.ClientSession(connector=conn,
                                     timeout=aiohttp.ClientTimeout(total=300)) as http:
        async def agentconfig_post(payload):
            async with http.post(f"https://{GW_HOST}/api/agentconfig", json=payload,
                                 headers={"X-API-Key": key}) as r:
                body = {}
                try:
                    body = await r.json()
                except Exception:
                    pass
                return r.status, body

        async def agentconfig_get():
            async with http.get(f"https://{GW_HOST}/api/agentconfig",
                                headers={"X-API-Key": key}) as r:
                body = {}
                try:
                    body = await r.json()
                except Exception:
                    pass
                return r.status, body

        # 先恢复默认再开测(上一轮遗留的自定义配置会污染 T1 基线)
        st, body = await agentconfig_post({
            "persona_name": "小歌",
            "persona_traits": "真诚、贴心、有分寸，像个老朋友：先接住对方的情绪和需求，再帮他把事办了。\n"
                              "但不啰嗦：说话利落，先给最想要的那句，再按需补一点。",
            "tts_voice": "longxiaochun_v3",
        })
        print(f"[reset] POST /api/agentconfig → {st} {body}", flush=True)

        body = {
            "device_id": DEVICE,
            "credential": {"key_id": "web-panel", "signature": "debug-session"},
            "caps": ["audio", "text", "cmd", "state"],
            "prefs": {"welcome.enabled": False},
            "connect_reason": "call_button",
            "audio_format": {"sample_rate": 16000, "channels": 1,
                             "sample_format": "int16le"},
            "client_version": "web-panel-r5.2.2",
        }
        async with http.post(f"https://{GW_HOST}/create_session", json=body,
                             headers={"X-API-Key": key}) as resp:
            data = await resp.json()
            print(f"[create_session] {resp.status} sid={data.get('session_id')}", flush=True)
        token, sid, trace = data["access_token"], data["session_id"], data.get("trace_id")
        async with http.ws_connect(
                f"wss://{GW_HOST}/debug/ws/session",
                params={"access_token": token}, ssl=sslctx,
                max_msg_size=16 * 1024 * 1024) as ws:
            await ws.send_json({
                "type": "ctrl.hello", "trace_id": trace, "session_id": sid,
                "proto": 2, "role": "panel", "device_id": DEVICE,
                "caps": data.get("granted_caps") or ["audio", "text", "cmd", "state"],
                "prefs": {"welcome.enabled": False},
            })

            events = []  # (ts, reply_text)

            async def reader():
                try:
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            j = json.loads(msg.data)
                            if j.get("type") == "data.reply":
                                txt = j.get("text") or ""
                                events.append((time.time(), txt))
                                print(f"<< reply: {txt[:120]}", flush=True)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED,
                                          aiohttp.WSMsgType.ERROR):
                            break
                except asyncio.CancelledError:
                    pass

            rt = asyncio.create_task(reader())
            await asyncio.sleep(2.0)

            marks = []  # (label, ts_before, ts_after)

            async def turn(label, text):
                print(f"\n[turn >>] ({label}) {text}", flush=True)
                t0 = time.time()
                n_before = len(events)
                await ws.send_json({"type": "data.text", "text": text})
                deadline = t0 + 40
                while len(events) < n_before + 1 and time.time() < deadline:
                    await asyncio.sleep(0.5)
                if len(events) < n_before + 1:
                    print(f"[warn] {label}: 40s 内没等到回复", flush=True)
                await asyncio.sleep(DRAIN_S)
                marks.append((label, t0, time.time()))

            async def turn_with_post(label, text, payload):
                """先 POST 配置再问话:验证在途会话下一轮热应用。"""
                print(f"\n[config >>] ({label}) {payload}", flush=True)
                t0 = time.time()
                st, body = await agentconfig_post(payload)
                check(f"{label}_post_ok", st == 200 and body.get("ok") is True,
                      f"POST {st} {body}")
                await turn(label, text)
                return t0

            await turn("T1_identity_default", "你是谁呀？简单介绍一下自己")
            await turn_with_post(
                "T2_identity_custom", "你是谁呀？简单介绍一下自己",
                {"persona_name": "糖糖",
                 "persona_traits": "活泼爱笑，称呼自己为糖糖，回答开头先热情打个招呼。",
                 "tts_voice": "longanwen_v3"})
            st, get_body = await agentconfig_get()
            check("GET_after_change",
                  st == 200 and get_body.get("persona_name") == "糖糖"
                  and get_body.get("tts_voice") == "longanwen_v3"
                  and len(get_body.get("voices") or []) >= 4,
                  f"GET {st} name={get_body.get('persona_name')} "
                  f"voice={get_body.get('tts_voice')} voices={len(get_body.get('voices') or [])}")
            await turn_with_post(
                "T3_identity_restore", "你是谁呀？简单介绍一下自己",
                {"persona_name": "小歌",
                 "persona_traits": "真诚、贴心、有分寸，像个老朋友：先接住对方的情绪和需求，再帮他把事办了。\n"
                                   "但不啰嗦：说话利落，先给最想要的那句，再按需补一点。",
                 "tts_voice": "longxiaochun_v3"})

            rt.cancel()
            try:
                await rt
            except asyncio.CancelledError:
                pass

    def frames(label):
        lo, hi = next((m[1], m[2]) for m in marks if m[0] == label)
        return [(ts, txt) for ts, txt in events if lo <= ts <= hi]

    def joined(label):
        return " ".join(txt for ts, txt in frames(label))

    def applied_in(label):
        lo, hi = next((m[1], m[2]) for m in marks if m[0] == label)
        return parse_log("AGENT_CONFIG_APPLIED", lo, hi)

    j1 = joined("T1_identity_default")
    check("T1_default_name", "小歌" in j1 and "糖糖" not in j1,
          f"基线答默认名 ({j1[:60]})")

    j2 = joined("T2_identity_custom")
    applied2 = applied_in("T2_identity_custom")
    check("T2_custom_name", "糖糖" in j2 and "小歌" not in j2,
          f"改名后答新名 ({j2[:60]})")
    check("T2_applied_logged",
          len(applied2) == 1 and "voice=longanwen_v3" in applied2[0]
          and "糖糖" in applied2[0],
          f"AGENT_CONFIG_APPLIED 恰好一次且带新名/新音色 ({applied2})")

    j3 = joined("T3_identity_restore")
    applied3 = applied_in("T3_identity_restore")
    check("T3_restored_name", "小歌" in j3,
          f"恢复后答默认名 ({j3[:60]})")
    check("T3_applied_logged",
          len(applied3) == 1 and "voice=longxiaochun_v3" in applied3[0],
          f"AGENT_CONFIG_APPLIED 恢复默认 ({applied3})")

    all_replies = " ".join(txt for ts, txt in events)
    check("SECRET", all(w not in all_replies for w in SECRET_WORDS),
          "全程不提接口/API/来源(人设保密)")

    print(f"\n[result] {'ALL PASS' if not fails else f'{len(fails)} FAIL'}", flush=True)
    for f in fails:
        print(f"  FAIL {f}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
