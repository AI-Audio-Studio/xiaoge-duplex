#!/usr/bin/env python3
"""
小歌语音系统 — 10 并发基准测试
测试模块：FunASR (ASR)、KWS、Turn Detector (判停)、LLM、TTS (CosyVoice)

运行方式（在服务器 xiaoge-duplex-main 目录下）：
    .venv/bin/python tests/benchmark_concurrent.py [funasr|kws|turn|llm|tts|all]

示例：
    .venv/bin/python tests/benchmark_concurrent.py all       # 全部测试
    .venv/bin/python tests/benchmark_concurrent.py funasr   # 只测 FunASR
    .venv/bin/python tests/benchmark_concurrent.py llm tts  # 测 LLM 和 TTS
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import ssl
import statistics
import struct
import sys
import time
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# ──────────────────────────────────────────────
# 配置（与服务器 .env 一致）
# ──────────────────────────────────────────────
FUNASR_WS_URL     = os.getenv("FUNASR_WS_URL",    "wss://127.0.0.1:10090")
FUNASR_VERIFY_SSL = os.getenv("FUNASR_VERIFY_SSL", "false").lower() != "true"

LLM_BASE_URL  = os.getenv("QWEN_BASE_URL", "https://127.0.0.1:10092/llm/v1")
LLM_API_KEY   = os.getenv("QWEN_API_KEY",  "EMPTY")
LLM_MODEL     = os.getenv("QWEN_MODEL",    "Qwen3-4B")
LLM_VERIFY_SSL = os.getenv("QWEN_VERIFY_SSL", "false").lower() != "true"

DASHSCOPE_API_KEY   = os.getenv("DASHSCOPE_API_KEY",  "")
COSYVOICE_MODEL     = os.getenv("COSYVOICE_MODEL",    "cosyvoice-v3-flash")
COSYVOICE_VOICE     = os.getenv("COSYVOICE_VOICE",    "longxiaochun_v3")

KWS_MODEL_DIR = os.getenv(
    "XIAOGE_KWS_MODEL_DIR",
    str(Path(__file__).parent.parent / "models" / "kws"
        / "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"),
)

TURN_MODEL_CACHE = Path.home() / ".cache/huggingface/hub" \
    / "models--livekit--turn-detector"

CONCURRENCY = 10   # 默认并发数，可被命令行 --concurrency N 覆盖
SAMPLE_RATE = 16000
AUDIO_SECS  = 3    # 测试音频时长（秒）

# ──────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────

@dataclass
class BenchResult:
    name: str
    ok: list[float] = field(default_factory=list)   # 成功请求延迟（秒）
    errors: list[str] = field(default_factory=list)

    def add_ok(self, latency: float) -> None:
        self.ok.append(latency)

    def add_err(self, msg: str) -> None:
        self.errors.append(msg)

    def report(self) -> str:
        total = len(self.ok) + len(self.errors)
        if not self.ok:
            lines = [
                f"\n{'='*60}",
                f"  [{self.name}]  总计: {total}  成功: 0  失败: {len(self.errors)}",
                f"  错误示例: {self.errors[:3]}",
                f"{'='*60}",
            ]
            return "\n".join(lines)
        s = sorted(self.ok)
        p = lambda pct: s[min(int(len(s) * pct / 100), len(s) - 1)]
        lines = [
            f"\n{'='*60}",
            f"  [{self.name}]",
            f"  并发数: {total}  成功: {len(self.ok)}  失败: {len(self.errors)}",
            f"  延迟(秒)  min={s[0]:.3f}  avg={statistics.mean(s):.3f}"
            f"  p50={p(50):.3f}  p90={p(90):.3f}  p99={p(99):.3f}  max={s[-1]:.3f}",
        ]
        if self.errors:
            lines.append(f"  错误: {self.errors[:3]}")
        lines.append(f"{'='*60}")
        return "\n".join(lines)


def gen_pcm_sine(secs: float = AUDIO_SECS, rate: int = SAMPLE_RATE, freq: float = 440.0) -> bytes:
    """生成 16-bit PCM 正弦波（模拟说话音频）。"""
    n = int(secs * rate)
    samples = [int(32767 * 0.3 * math.sin(2 * math.pi * freq * i / rate)) for i in range(n)]
    return struct.pack(f"<{n}h", *samples)


def _ssl_ctx_no_verify() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def hdr(title: str) -> None:
    print(f"\n{'─'*60}")
    print(f"  开始测试：{title}  (并发={CONCURRENCY})")
    print(f"{'─'*60}")


async def run_concurrent(
    tasks: list[Callable[[], object]],
    result: BenchResult,
) -> None:
    """并发执行异步任务列表，收集结果。"""
    async def wrap(fn: Callable) -> None:
        t0 = time.perf_counter()
        try:
            await fn()
            result.add_ok(time.perf_counter() - t0)
        except Exception as e:
            result.add_err(f"{type(e).__name__}: {e}")

    await asyncio.gather(*[wrap(fn) for fn in tasks])


# ──────────────────────────────────────────────
# 1. FunASR — 2pass 流式 WebSocket
# ──────────────────────────────────────────────

async def test_funasr() -> BenchResult:
    hdr("FunASR (wss 2pass 流式)")
    import aiohttp

    pcm = gen_pcm_sine(AUDIO_SECS)
    CHUNK = 9600       # 300ms@16kHz = 9600 bytes
    ssl_ctx = _ssl_ctx_no_verify() if FUNASR_VERIFY_SSL else None
    result = BenchResult("FunASR")

    async def one_session(idx: int) -> None:
        init_payload = json.dumps({
            "mode": "2pass",
            "chunk_size": [5, 10, 5],
            "chunk_interval": 10,
            "wav_name": f"bench_{idx:02d}",
            "wav_format": "pcm",
            "audio_fs": SAMPLE_RATE,
            "is_speaking": True,
            "itn": False,
        }, ensure_ascii=False)

        timeout = aiohttp.ClientWSTimeout(ws_receive=20.0)
        async with aiohttp.ClientSession() as sess:
            async with sess.ws_connect(
                FUNASR_WS_URL, ssl=ssl_ctx, heartbeat=20, timeout=timeout
            ) as ws:
                await ws.send_str(init_payload)
                # 分块发送（模拟实时流）
                for i in range(0, len(pcm), CHUNK):
                    await ws.send_bytes(pcm[i: i + CHUNK])
                    await asyncio.sleep(0)        # 让出事件循环
                await ws.send_str(json.dumps({"is_speaking": False}))

                # 等待 final 结果
                transcript = ""
                mode = ""
                deadline = time.monotonic() + 15
                async for msg in ws:
                    if time.monotonic() > deadline:
                        break
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        d = json.loads(msg.data)
                        mode = d.get("mode", "")
                        if d.get("text"):
                            transcript = d["text"]
                        if mode == "2pass-offline" and d.get("is_final"):
                            break
                    elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                        break

                print(f"  [{idx:02d}] mode={mode} transcript={transcript!r:.40s}")

    tasks = [lambda i=i: one_session(i) for i in range(CONCURRENCY)]
    await run_concurrent(tasks, result)
    return result


# ──────────────────────────────────────────────
# 2. KWS — sherpa-onnx 关键词检测（线程并发）
# ──────────────────────────────────────────────

def test_kws_sync(result: BenchResult) -> None:
    """KWS 是同步推理，用线程池并发测试。"""
    hdr("KWS (sherpa-onnx 本地推理)")
    try:
        import sherpa_onnx  # type: ignore
    except ImportError:
        result.add_err("sherpa_onnx 未安装")
        return

    model_dir = Path(KWS_MODEL_DIR)
    encoder  = model_dir / "encoder-epoch-13-avg-2-chunk-8-left-64.onnx"
    decoder  = model_dir / "decoder-epoch-13-avg-2-chunk-8-left-64.onnx"
    joiner   = model_dir / "joiner-epoch-13-avg-2-chunk-8-left-64.onnx"
    kw_file  = model_dir / "generated-keywords.txt"

    for f in [encoder, decoder, joiner, kw_file]:
        if not f.exists():
            result.add_err(f"模型文件缺失: {f.name}")
            return

    # sherpa-onnx KWS 对象不能跨线程共享，每线程独立创建
    def run_one(idx: int) -> None:
        t0 = time.perf_counter()
        try:
            kws = sherpa_onnx.KeywordSpotter(
                tokens=str(model_dir / "tokens.txt"),
                encoder=str(encoder),
                decoder=str(decoder),
                joiner=str(joiner),
                keywords_file=str(kw_file),
                num_threads=1,
                provider="cpu",
                keywords_threshold=0.12,
                keywords_score=1.0,
            )
            stream = kws.create_stream()

            # 按 chunk 喂入音频，模拟实时流（sherpa-onnx 标准驱动方式）
            import math as _math
            n = int(AUDIO_SECS * SAMPLE_RATE)
            chunk_size = 1600   # 100ms @16kHz
            kw_hit = ""
            for start in range(0, n, chunk_size):
                chunk_samples = [
                    0.3 * _math.sin(2 * _math.pi * 440 * i / SAMPLE_RATE)
                    for i in range(start, min(start + chunk_size, n))
                ]
                stream.accept_waveform(SAMPLE_RATE, chunk_samples)
                while kws.is_ready(stream):
                    kws.decode_stream(stream)
                r = kws.get_result(stream)
                kw_text = str(r).strip()
                if kw_text:
                    kw_hit = kw_text

            latency = time.perf_counter() - t0
            result.add_ok(latency)
            print(f"  [{idx:02d}] 耗时={latency:.3f}s  命中关键词={'【' + kw_hit + '】' if kw_hit else '(无，正弦波无关键词正常)'}")
        except Exception as e:
            result.add_err(f"{type(e).__name__}: {e}")

    threads = [threading.Thread(target=run_one, args=(i,)) for i in range(CONCURRENCY)]
    t_start = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print(f"  总墙钟时间: {time.perf_counter()-t_start:.3f}s  (含串行创建开销)")


async def test_kws() -> BenchResult:
    result = BenchResult("KWS")
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, test_kws_sync, result)
    return result


# ──────────────────────────────────────────────
# 3. Turn Detector — ONNX 判停模型
# ──────────────────────────────────────────────

async def test_turn_detector() -> BenchResult:
    hdr("Turn Detector (ONNX 判停模型)")
    result = BenchResult("TurnDetector")

    # 找 onnx 模型文件
    onnx_path: Path | None = None
    for snap in sorted(TURN_MODEL_CACHE.glob("snapshots/*/onnx/model_q8.onnx")):
        onnx_path = snap
    if onnx_path is None:
        result.add_err(f"未找到 model_q8.onnx，检查 {TURN_MODEL_CACHE}")
        return result

    # 找 tokenizer 目录
    tokenizer_dir = onnx_path.parent.parent  # snapshots/<hash>/

    print(f"  模型: {onnx_path}")
    print(f"  大小: {onnx_path.stat().st_size / 1024 / 1024:.1f} MB")

    try:
        import onnxruntime as ort
        from transformers import AutoTokenizer  # type: ignore
    except ImportError as e:
        result.add_err(f"依赖缺失: {e}")
        return result

    # 加载模型（进程级共享，只加载一次）
    print("  正在加载 ONNX 模型（首次较慢）...")
    t_load = time.perf_counter()
    sess_opts = ort.SessionOptions()
    sess_opts.intra_op_num_threads = 1
    sess_opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    onnx_session = ort.InferenceSession(
        str(onnx_path),
        sess_options=sess_opts,
        providers=["CPUExecutionProvider"],
    )
    tokenizer = AutoTokenizer.from_pretrained(str(tokenizer_dir))
    print(f"  模型加载耗时: {time.perf_counter()-t_load:.2f}s")

    # 测试用对话（中文场景）
    TEST_MESSAGES = [
        {"role": "user",      "content": "今天天气怎么样"},
        {"role": "assistant", "content": "今天北京晴天，气温25度左右，适合出行"},
        {"role": "user",      "content": "那明天呢"},
    ]

    def infer_one(idx: int) -> tuple[int, float, float]:
        t0 = time.perf_counter()
        # 构建输入
        text = tokenizer.apply_chat_template(
            TEST_MESSAGES, tokenize=False, add_generation_prompt=False
        )
        inputs = tokenizer(text, return_tensors="np", truncation=True, max_length=512)
        # ONNX 推理（线程安全）
        # 模型只需 input_ids，输出 prob 是直接的 EOU 概率（已经过 sigmoid）
        prob_out = onnx_session.run(
            None,
            {"input_ids": inputs["input_ids"]},
        )[0]
        eou_prob = float(prob_out.flat[0])
        return idx, time.perf_counter() - t0, eou_prob

    # 线程并发推理
    results_raw: list[tuple[int, float, float]] = []
    lock = threading.Lock()

    def run_thread(idx: int) -> None:
        r = infer_one(idx)
        with lock:
            results_raw.append(r)

    threads = [threading.Thread(target=run_thread, args=(i,)) for i in range(CONCURRENCY)]
    t_wall = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - t_wall

    for idx, lat, prob in sorted(results_raw):
        decision = "说完✅" if prob > 0.5 else "继续⏳"
        print(f"  [{idx:02d}] 耗时={lat:.3f}s  EOU概率={prob:.3f}  判断={decision}")
        result.add_ok(lat)

    if results_raw:
        print(f"  总墙钟时间: {wall:.3f}s  (理论最优={max(r[1] for r in results_raw):.3f}s)")
    else:
        print(f"  总墙钟时间: {wall:.3f}s  (全部失败，见上方错误)")
    return result


# ──────────────────────────────────────────────
# 4. LLM — vLLM / Qwen3-4B
# ──────────────────────────────────────────────

async def test_llm() -> BenchResult:
    hdr(f"LLM ({LLM_MODEL} via {LLM_BASE_URL})")
    import aiohttp

    result = BenchResult("LLM")
    ssl_ctx = _ssl_ctx_no_verify() if LLM_VERIFY_SSL else None

    # 测试用提示（不同问题，避免 KV-cache 命中掩盖并发能力）
    PROMPTS = [
        "用一句话解释什么是人工智能",
        "北京的著名景点有哪些，列举三个",
        "水的化学式是什么",
        "请用一句话描述春天",
        "1加1等于几",
        "太阳系有几颗行星",
        "用一句话说明量子计算的特点",
        "中国的首都是哪里",
        "请介绍一下Python编程语言",
        "天空为什么是蓝色的",
    ]

    async def one_request(idx: int) -> None:
        payload = {
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": PROMPTS[idx % len(PROMPTS)]}],
            "max_tokens": 30,
            "stream": False,
            "temperature": 0.1,
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LLM_API_KEY}",
        }
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(
                f"{LLM_BASE_URL}/chat/completions",
                json=payload, headers=headers, ssl=ssl_ctx,
            ) as resp:
                body = await resp.json(content_type=None)
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}: {body}")
                content = body["choices"][0]["message"]["content"]
                usage = body.get("usage", {})
                total_tok = usage.get("total_tokens", "?")
                prompt_tok = usage.get("prompt_tokens", "?")
                comp_tok = usage.get("completion_tokens", "?")
                print(f"  [{idx:02d}] tokens={prompt_tok}+{comp_tok}={total_tok}"
                      f"  reply={content!r:.35s}")

    tasks = [lambda i=i: one_request(i) for i in range(CONCURRENCY)]
    await run_concurrent(tasks, result)
    return result


# ──────────────────────────────────────────────
# 5. TTS — CosyVoice (DashScope API)
# ──────────────────────────────────────────────

async def test_tts() -> BenchResult:
    hdr(f"TTS (CosyVoice {COSYVOICE_MODEL} / {COSYVOICE_VOICE})")
    result = BenchResult("TTS-CosyVoice")

    if not DASHSCOPE_API_KEY:
        result.add_err("DASHSCOPE_API_KEY 未设置")
        return result

    TEXTS = [
        "今天天气真不错",
        "人工智能正在改变世界",
        "请问有什么我可以帮您的吗",
        "北京是中国的首都",
        "欢迎使用小歌语音助手",
        "明天的会议安排在上午十点",
        "请稍等，我马上为您查询",
        "好的，我明白了您的需求",
        "这个问题非常有趣",
        "感谢您的耐心等待",
    ]

    # 判断使用 dashscope SDK 还是 HTTP 接口
    # 实际生产用的是 dashscope SpeechSynthesizer (streaming)
    # 这里用非流式请求测并发

    def synth_one_sync(idx: int) -> tuple[int, float, int]:
        """同步 DashScope TTS 调用（SDK 是同步的）。"""
        import dashscope
        from dashscope.audio.tts_v2 import SpeechSynthesizer  # type: ignore

        dashscope.api_key = DASHSCOPE_API_KEY
        t0 = time.perf_counter()

        chunks: list[bytes] = []
        synthesizer = SpeechSynthesizer(
            model=COSYVOICE_MODEL,
            voice=COSYVOICE_VOICE,
        )
        audio = synthesizer.call(TEXTS[idx % len(TEXTS)])
        latency = time.perf_counter() - t0
        audio_bytes = audio if isinstance(audio, bytes) else b""
        return idx, latency, len(audio_bytes)

    # 用线程池并发调用同步 SDK
    results_raw: list[tuple[int, float, int]] = []
    errors: list[str] = []
    lock = threading.Lock()

    def run_thread(idx: int) -> None:
        try:
            r = synth_one_sync(idx)
            with lock:
                results_raw.append(r)
                result.add_ok(r[1])
        except Exception as e:
            with lock:
                errors.append(str(e))
                result.add_err(f"[{idx}] {type(e).__name__}: {e}")

    threads = [threading.Thread(target=run_thread, args=(i,)) for i in range(CONCURRENCY)]
    t_wall = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - t_wall

    for idx, lat, nb in sorted(results_raw):
        print(f"  [{idx:02d}] 耗时={lat:.3f}s  音频={nb//1024}KB  text={TEXTS[idx%len(TEXTS)]!r}")
    if errors:
        print(f"  错误: {errors[:3]}")
    print(f"  总墙钟时间: {wall:.3f}s")
    return result


# ──────────────────────────────────────────────
# 主入口
# ──────────────────────────────────────────────

ALL_TESTS = {
    "funasr": test_funasr,
    "kws":    test_kws,
    "turn":   test_turn_detector,
    "llm":    test_llm,
    "tts":    test_tts,
}

async def main() -> None:
    global CONCURRENCY
    args = sys.argv[1:]

    # 解析 --concurrency N
    if "--concurrency" in args:
        idx = args.index("--concurrency")
        CONCURRENCY = int(args[idx + 1])
        args = args[:idx] + args[idx + 2:]

    targets = args if args else ["all"]
    if "all" in targets:
        targets = list(ALL_TESTS.keys())

    unknown = [t for t in targets if t not in ALL_TESTS]
    if unknown:
        print(f"未知测试项: {unknown}，可用: {list(ALL_TESTS.keys())}")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"  小歌语音系统  10 并发基准测试")
    print(f"  测试项: {', '.join(targets)}")
    print(f"  并发数: {CONCURRENCY}  测试音频: {AUDIO_SECS}s @ {SAMPLE_RATE}Hz")
    print(f"{'='*60}")

    results: list[BenchResult] = []
    for name in targets:
        fn = ALL_TESTS[name]
        r = await fn()
        results.append(r)
        print(r.report())

    # 汇总
    print(f"\n{'='*60}")
    print("  汇总")
    print(f"{'='*60}")
    for r in results:
        total = len(r.ok) + len(r.errors)
        ok = len(r.ok)
        avg = statistics.mean(r.ok) if r.ok else float("nan")
        p90 = sorted(r.ok)[int(len(r.ok)*0.9)] if r.ok else float("nan")
        print(f"  {r.name:<20} 成功率={ok}/{total}  avg={avg:.3f}s  p90={p90:.3f}s")


if __name__ == "__main__":
    asyncio.run(main())
