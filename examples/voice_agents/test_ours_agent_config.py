"""小歌可热更配置(音色+人设)单测 — 2026-09-10 agent_config。

三层:
- 纯 config_store(sanitize/load/save/persona_instructions):不依赖 livekit,
  本地/服务器都能跑。AGENT_CONFIG_PATH 在 import 时从 env 解析 → 必须
  monkeypatch 模块属性,不是环境变量。
- web_ui_agent(_build_agent_instructions / _maybe_apply_agent_config 轮次钩子):
  需 livekit,仅服务器 venv 可跑(本地无 livekit → 整类 skip)。
- 真实 app.backends 音色优先级(显式参数 > agent_config.json(保存过才生效)
  > env COSYVOICE_VOICE > 默认):需 httpx/openai/providers,同样仅服务器跑。

重点回归:
- sanitize 部分提交(None 字段)必须保留底值——防"只改音色把人设写成None"。
- 文件里 null 字段回落默认——防 str(None) 把人设名字写成"None"。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import types
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
# 服务器:测试就躺在 voice_agents 目录里(_HERE 即包根);本地预检:镜像目录。
for _p in (_HERE, _HERE / "remote_bak_0910"):
    if (_p / "app" / "config_store.py").exists():
        sys.path.insert(0, str(_p))
        break

import app.config_store as config_store  # noqa: E402  纯 stdlib

# 真实 backends(音色优先级用):先于 stub import 留下真身。
try:
    import app.backends as real_backends  # noqa: E402
except Exception:  # pragma: no cover - 本地预检环境缺 httpx/openai
    real_backends = None

# web_ui_agent import 前的 stub 舞(与 test_ours_weather_tool 一致)。
backends = types.ModuleType("app.backends")
backends.build_llm = lambda: None
backends.build_semantic_llm = lambda: None
backends.build_tts = lambda: None
backends.build_stt = lambda: None
backends.STT_BACKENDS = {}
backends.TTS_BACKENDS = {}
backends.backend_tabs_html = lambda *args, **kwargs: ""
backends.make_stt_backend = lambda *args, **kwargs: None
backends.make_tts_backend = lambda *args, **kwargs: None
sys.modules["app.backends"] = backends
providers_config = types.ModuleType("providers.config")
providers_config.funasr_hotwords = lambda: {}
sys.modules.setdefault("providers.config", providers_config)
silero = types.ModuleType("livekit.plugins.silero")
silero.VAD = types.SimpleNamespace(load=lambda *args, **kwargs: None)
sys.modules.setdefault("livekit.plugins.silero", silero)

try:
    import web_ui_agent  # noqa: E402
except Exception:  # pragma: no cover - 本地无 livekit
    web_ui_agent = None


@pytest.fixture
def cfg_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """AGENT_CONFIG_PATH 指到 tmp + 清 mtime 缓存(测试间互不污染)。"""
    p = tmp_path / "agent_config.json"
    monkeypatch.setattr(config_store, "AGENT_CONFIG_PATH", p)
    monkeypatch.setattr(config_store, "_cache", None)
    return p


# ── sanitize:整次提交要么全收要么全拒 ──────────────────────────────────


class TestSanitize:
    def test_full_update(self):
        cfg, err = config_store.sanitize_config("糖糖", "活泼", "longyan_v3",
                                                config_store.DEFAULT_CONFIG)
        assert err is None
        assert cfg.persona_name == "糖糖"
        assert cfg.persona_traits == "活泼"
        assert cfg.tts_voice == "longyan_v3"

    def test_partial_voice_only_keeps_persona(self):
        """回归:只改音色(None=None)时人设字段必须保留底值,不许变 None。"""
        base = config_store.AgentConfig(persona_name="糖糖",
                                        persona_traits="沉稳",
                                        tts_voice="longxiaochun_v3")
        cfg, err = config_store.sanitize_config(None, None, "longyan_v3", base)
        assert err is None
        assert cfg.persona_name == "糖糖"
        assert cfg.persona_traits == "沉稳"
        assert cfg.tts_voice == "longyan_v3"

    def test_partial_name_only_keeps_voice(self):
        base = config_store.AgentConfig(persona_name="小歌",
                                        persona_traits="原性格",
                                        tts_voice="longanlang_v3")
        cfg, err = config_store.sanitize_config("糖糖", None, None, base)
        assert err is None
        assert cfg.persona_name == "糖糖"
        assert cfg.persona_traits == "原性格"
        assert cfg.tts_voice == "longanlang_v3"

    def test_all_none_is_identity(self):
        base = config_store.AgentConfig(persona_name="糖糖", persona_traits="x",
                                        tts_voice="longanli_v3")
        cfg, err = config_store.sanitize_config(None, None, None, base)
        assert err is None and cfg == base

    def test_unknown_voice_rejects_whole_submission(self):
        base = config_store.AgentConfig(persona_name="糖糖", persona_traits="x",
                                        tts_voice="longyan_v3")
        cfg, err = config_store.sanitize_config("新名字", "新性格", "no-such-voice", base)
        assert err is not None and "voice" in err
        assert cfg == base  # 整次拒绝,不会改一半

    def test_empty_name_rejected(self):
        cfg, err = config_store.sanitize_config("   ", None, None,
                                                config_store.DEFAULT_CONFIG)
        assert err is not None and cfg == config_store.DEFAULT_CONFIG

    def test_name_too_long_rejected(self):
        cfg, err = config_store.sanitize_config("名" * 31, None, None,
                                                config_store.DEFAULT_CONFIG)
        assert err is not None and cfg == config_store.DEFAULT_CONFIG

    def test_traits_empty_rejected(self):
        cfg, err = config_store.sanitize_config(None, "  \n ", None,
                                                config_store.DEFAULT_CONFIG)
        assert err is not None and cfg == config_store.DEFAULT_CONFIG

    def test_traits_too_long_rejected(self):
        cfg, err = config_store.sanitize_config(None, "长" * 2001, None,
                                                config_store.DEFAULT_CONFIG)
        assert err is not None and cfg == config_store.DEFAULT_CONFIG

    def test_non_string_types_rejected(self):
        for name, traits, voice in ((123, None, None), (None, 456, None), (None, None, 789)):
            cfg, err = config_store.sanitize_config(name, traits, voice,
                                                    config_store.DEFAULT_CONFIG)
            assert err is not None and cfg == config_store.DEFAULT_CONFIG

    def test_values_are_stripped(self):
        cfg, err = config_store.sanitize_config("  糖糖  ", "  活泼  ", "longyan_v3 ",
                                                config_store.DEFAULT_CONFIG)
        assert err is None
        assert cfg.persona_name == "糖糖"
        assert cfg.persona_traits == "活泼"


# ── load/save:文件缺失/损坏回落默认,save→load 原子往返 ─────────────────


class TestLoadSave:
    def test_missing_file_returns_default(self, cfg_path: Path):
        assert config_store.load_agent_config() == config_store.DEFAULT_CONFIG

    def test_save_then_load_roundtrip(self, cfg_path: Path):
        cfg = config_store.AgentConfig(persona_name="糖糖",
                                       persona_traits="第一行\n第二行",
                                       tts_voice="longanlang_v3")
        config_store.save_agent_config(cfg)
        assert config_store.load_agent_config() == cfg
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert raw["persona_name"] == "糖糖"
        assert raw["tts_voice"] == "longanlang_v3"
        assert "updated_at" in raw

    def test_corrupt_json_falls_back_to_default(self, cfg_path: Path):
        cfg_path.write_text("{not json", encoding="utf-8")
        assert config_store.load_agent_config() == config_store.DEFAULT_CONFIG

    def test_non_object_root_falls_back_to_default(self, cfg_path: Path):
        cfg_path.write_text("[1, 2]", encoding="utf-8")
        assert config_store.load_agent_config() == config_store.DEFAULT_CONFIG

    def test_null_fields_fall_back_to_default(self, cfg_path: Path):
        """回归:文件里 null 字段不许变成字符串"None"。"""
        cfg_path.write_text(
            json.dumps({"persona_name": None, "persona_traits": None, "tts_voice": None}),
            encoding="utf-8")
        assert config_store.load_agent_config() == config_store.DEFAULT_CONFIG

    def test_invalid_voice_in_file_falls_back_to_default(self, cfg_path: Path):
        cfg_path.write_text(json.dumps({
            "persona_name": "糖糖", "persona_traits": "活泼", "tts_voice": "bad-voice"}),
            encoding="utf-8")
        assert config_store.load_agent_config() == config_store.DEFAULT_CONFIG

    def test_mtime_change_reloads(self, cfg_path: Path):
        cfg_path.write_text(json.dumps({
            "persona_name": "糖糖", "persona_traits": "活泼",
            "tts_voice": "longyan_v3"}), encoding="utf-8")
        assert config_store.load_agent_config().persona_name == "糖糖"
        cfg_path.write_text(json.dumps({
            "persona_name": "安安", "persona_traits": "沉稳",
            "tts_voice": "longanlang_v3"}), encoding="utf-8")
        # 强制 mtime 与上次不同(防文件系统时间戳粒度撞车)
        os.utime(cfg_path, (1_800_000_000, 1_800_000_000))
        cfg = config_store.load_agent_config()
        assert cfg.persona_name == "安安" and cfg.tts_voice == "longanlang_v3"


class TestLoadSavedRaw:
    """load_saved_agent_config:区分"没保存过"(None)与"保存过"(配置)。

    backends 的 env 兜底靠它——旧版 load_agent_config 缺文件回默认值
    (非空音色),env COSYVOICE_VOICE 永远赢不了。
    """

    def test_missing_file_returns_none(self, cfg_path: Path):
        assert config_store.load_saved_agent_config() is None
        # 公共门面行为不变:缺文件仍回默认
        assert config_store.load_agent_config() == config_store.DEFAULT_CONFIG

    def test_saved_file_returns_config(self, cfg_path: Path):
        cfg = config_store.AgentConfig(persona_name="糖糖",
                                       persona_traits="活泼",
                                       tts_voice="longyan_v3")
        config_store.save_agent_config(cfg)
        assert config_store.load_saved_agent_config() == cfg

    def test_corrupt_file_returns_none(self, cfg_path: Path):
        cfg_path.write_text("{not json", encoding="utf-8")
        assert config_store.load_saved_agent_config() is None
        assert config_store.load_agent_config() == config_store.DEFAULT_CONFIG


# ── persona_instructions:可编辑块拼装 ──────────────────────────────────


class TestPersonaInstructions:
    def test_default_block(self):
        s = config_store.persona_instructions(config_store.DEFAULT_CONFIG)
        assert s.startswith("# 你是谁\n你叫小歌，")
        assert "# 性格" in s
        assert "- 真诚、贴心" in s  # 多行性格逐行加 "- "

    def test_multiline_traits_each_line_bulleted(self):
        cfg = config_store.AgentConfig(persona_name="糖糖",
                                       persona_traits="第一行\n  第二行  \n\n")
        s = config_store.persona_instructions(cfg)
        assert "你叫糖糖" in s
        assert "- 第一行\n- 第二行" in s
        assert "小歌" not in s

    def test_whitespace_traits_gets_fallback(self):
        cfg = config_store.AgentConfig(persona_name="糖糖", persona_traits=" \n ")
        s = config_store.persona_instructions(cfg)
        assert "- 真诚、贴心，像个老朋友。" in s


# ── web_ui_agent:instructions 拼装 + 轮次钩子热应用(需 livekit) ─────────

pytestmark_web = pytest.mark.skipif(web_ui_agent is None, reason="no livekit locally")


@pytest.fixture
def web_env(cfg_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """web_ui_agent 侧 monkeypatch:turn log 记录 + make_tts_backend 假件。"""
    rec = {"logs": [], "made": [], "switched": [], "closed": []}

    def fake_append_turn_log(line: str) -> None:
        rec["logs"].append(line)

    def fake_make_tts_backend(backend: str, voice: str | None = None):
        rec["made"].append((backend, voice))
        return f"FAKE_TTS:{voice}"

    class _FakeOldTTS:
        async def aclose(self) -> None:
            rec["closed"].append("old")

    class _FakeSwitchable:
        def switch_backend(self, new):
            rec["switched"].append(new)
            return _FakeOldTTS()

    monkeypatch.setattr(web_ui_agent, "_append_turn_log", fake_append_turn_log)
    monkeypatch.setattr(web_ui_agent, "make_tts_backend", fake_make_tts_backend)
    monkeypatch.setattr(web_ui_agent.runtime, "switchable_tts", _FakeSwitchable())
    monkeypatch.setattr(web_ui_agent.runtime, "tts_backend_key", "cosyvoice")
    rec["agent_loop"] = None
    return rec


def _new_agent(cfg):
    async def update_instructions(instructions: str) -> None:
        raise AssertionError("unexpected call")  # 默认炸,测试里按需替换

    agent = types.SimpleNamespace(update_instructions=update_instructions, _agent_cfg=cfg)
    agent._maybe_apply_agent_config = (
        web_ui_agent.VoiceAgent._maybe_apply_agent_config.__get__(agent)
    )
    # 2026-09-22 会话性格:真 _maybe_apply_agent_config 内部经 self 调静态方法
    # _effective_agent_config(cfg, persona_key)。staticmethod 经类访问即原函数,
    # 直接赋值即可(实例属性不走描述符,误用 __get__ 反而会多塞一个 self)。
    agent._effective_agent_config = web_ui_agent.VoiceAgent._effective_agent_config
    agent._apply_session_overrides = web_ui_agent.VoiceAgent._apply_session_overrides
    agent._duplex_off = (
        web_ui_agent.VoiceAgent._duplex_off.__get__(agent)
    )
    agent._voice_welcome_said = False  # 2026-09-16 welcome unification: read by handle_voice_welcome
    return agent


async def _noop_update(instructions: str) -> None:
    pass


@pytestmark_web
class TestBuildInstructions:
    def test_default_is_persona_block_plus_fixed(self):
        s = web_ui_agent._build_agent_instructions(config_store.DEFAULT_CONFIG)
        assert s.startswith("# 你是谁\n你叫小歌，")
        assert "# 性格" in s
        assert "# 保持本色" in s
        assert "# 身份与保密" in s  # 固定段(保密规则不进可编辑配置)

    def test_custom_name_replaces_in_fixed_block(self):
        cfg = config_store.AgentConfig(persona_name="糖糖",
                                       persona_traits="活泼",
                                       tts_voice="longyan_v3")
        s = web_ui_agent._build_agent_instructions(cfg)
        assert "糖糖" in s
        base = s.split("# 真实可用工具边界", 1)[0]
        assert "小歌" not in base  # 固定段里的"小歌"一并替换,人设自洽
        assert "以糖糖的身份" in base
        assert "小歌/歌尔产品知识库" in s  # 运行期能力护栏保留真实产品名

    def test_custom_traits_flow_through(self):
        cfg = config_store.AgentConfig(persona_name="糖糖",
                                       persona_traits="爱讲冷笑话",
                                       tts_voice="longxiaochun_v3")
        s = web_ui_agent._build_agent_instructions(cfg)
        assert "- 爱讲冷笑话" in s


@pytestmark_web
class TestHotApply:
    def test_no_change_is_noop(self, cfg_path: Path, web_env: dict):
        agent = _new_agent(config_store.DEFAULT_CONFIG)  # 文件缺失 → 默认 → 相等
        asyncio.run(agent._maybe_apply_agent_config())
        assert web_env["made"] == [] and web_env["switched"] == []
        assert web_env["logs"] == []

    def test_persona_change_updates_instructions_only(self, cfg_path: Path, web_env: dict):
        config_store.save_agent_config(config_store.AgentConfig(
            persona_name="糖糖", persona_traits="活泼", tts_voice="longxiaochun_v3"))
        agent = _new_agent(config_store.DEFAULT_CONFIG)
        applied = []

        async def collect(instructions: str) -> None:
            applied.append(instructions)

        agent.update_instructions = collect
        asyncio.run(agent._maybe_apply_agent_config())
        assert len(applied) == 1
        assert applied[0].startswith("# 你是谁\n你叫糖糖，")
        assert "# 保持本色" in applied[0]
        assert web_env["made"] == []  # 音色没变 → 不动 TTS
        assert agent._agent_cfg.persona_name == "糖糖"
        assert any("AGENT_CONFIG_APPLIED" in line for line in web_env["logs"])

    def test_voice_change_switches_cosyvoice(self, cfg_path: Path, web_env: dict):
        config_store.save_agent_config(config_store.AgentConfig(
            persona_name="小歌",
            persona_traits=config_store.DEFAULT_PERSONA_TRAITS,
            tts_voice="longanlang_v3"))
        agent = _new_agent(config_store.DEFAULT_CONFIG)
        agent.update_instructions = _noop_update
        asyncio.run(agent._maybe_apply_agent_config())
        assert web_env["made"] == [("cosyvoice", "longanlang_v3")]
        assert web_env["switched"] == ["FAKE_TTS:longanlang_v3"]
        assert web_env["closed"] == ["old"]  # 旧实例 aclose(真 CosyVoice 是 no-op)
        assert agent._agent_cfg.tts_voice == "longanlang_v3"

    def test_voice_change_skipped_when_backend_not_cosyvoice(
        self, cfg_path: Path, web_env: dict, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(web_ui_agent.runtime, "tts_backend_key", "qwen")
        config_store.save_agent_config(config_store.AgentConfig(
            persona_name="小歌",
            persona_traits=config_store.DEFAULT_PERSONA_TRAITS,
            tts_voice="longanlang_v3"))
        agent = _new_agent(config_store.DEFAULT_CONFIG)
        agent.update_instructions = _noop_update
        asyncio.run(agent._maybe_apply_agent_config())
        assert web_env["made"] == []  # qwen 后端没有音色概念,不换
        assert agent._agent_cfg.tts_voice == "longanlang_v3"  # 配置仍记账

    def test_apply_failure_is_swallowed(self, cfg_path: Path, web_env: dict):
        config_store.save_agent_config(config_store.AgentConfig(
            persona_name="糖糖", persona_traits="活泼", tts_voice="longyan_v3"))

        async def boom(instructions: str) -> None:
            raise RuntimeError("update_instructions down")

        agent = _new_agent(config_store.DEFAULT_CONFIG)
        agent.update_instructions = boom
        asyncio.run(agent._maybe_apply_agent_config())  # 不许抛
        assert agent._agent_cfg == config_store.DEFAULT_CONFIG  # 保持旧配置
        assert web_env["switched"] == []  # 人设都没应用成 → TTS 也不动


@pytestmark_web
class TestWelcomePreApply:
    """连接欢迎语前置热应用(2026-09-10 欢迎语音色跟手)。

    复连场景:用户改音色 → 重连 → VOICE_WELCOME 在轮次钩子之前播,
    必须由 handle_voice_welcome 先换 TTS 再开口。
    """

    def _welcome_agent(self, cfg):
        agent = _new_agent(cfg)
        agent.handle_voice_welcome = (
            web_ui_agent.VoiceAgent.handle_voice_welcome.__get__(agent)
        )
        return agent

    def test_welcome_applies_voice_before_saying(
        self, cfg_path: Path, web_env: dict, monkeypatch: pytest.MonkeyPatch
    ):
        config_store.save_agent_config(config_store.AgentConfig(
            persona_name="小歌",
            persona_traits=config_store.DEFAULT_PERSONA_TRAITS,
            tts_voice="longanlang_v3"))
        order = []  # 顺序守卫: applied 必须在 say 之前
        inner_log = web_ui_agent._append_turn_log

        def log_and_order(line: str) -> None:
            inner_log(line)
            order.append(("log", line))

        monkeypatch.setattr(web_ui_agent, "_append_turn_log", log_and_order)
        monkeypatch.setattr(web_ui_agent, "say_voice_welcome",
                            lambda voice=None: order.append(("say", voice)))
        agent = self._welcome_agent(config_store.DEFAULT_CONFIG)
        agent.update_instructions = _noop_update
        asyncio.run(agent.handle_voice_welcome())
        assert web_env["made"] == [("cosyvoice", "longanlang_v3")]  # 先换 TTS
        assert order == [
            ("log", "AGENT_CONFIG_APPLIED name='小歌' voice=longanlang_v3"
                    " duplex=on instructions=default persona=none"),  # 2026-09-22 起带会话性格标记
            ("say", "longanlang_v3"),  # 换完才说, 音色随埋点带出
        ]

    def test_welcome_no_change_says_current_voice(
        self, cfg_path: Path, web_env: dict, monkeypatch: pytest.MonkeyPatch
    ):
        said = []
        monkeypatch.setattr(web_ui_agent, "say_voice_welcome",
                            lambda voice=None: said.append(voice))
        agent = self._welcome_agent(config_store.DEFAULT_CONFIG)  # 文件缺失 → 相等
        agent.update_instructions = _noop_update
        asyncio.run(agent.handle_voice_welcome())
        assert web_env["made"] == [] and web_env["logs"] == []  # 零开销
        assert said == [config_store.DEFAULT_CONFIG.tts_voice]  # 欢迎语照说

    def test_welcome_apply_failure_still_says(
        self, cfg_path: Path, web_env: dict, monkeypatch: pytest.MonkeyPatch
    ):
        config_store.save_agent_config(config_store.AgentConfig(
            persona_name="糖糖", persona_traits="活泼", tts_voice="longyan_v3"))

        async def boom(instructions: str) -> None:
            raise RuntimeError("update_instructions down")

        said = []
        monkeypatch.setattr(web_ui_agent, "say_voice_welcome",
                            lambda voice=None: said.append(voice))
        agent = self._welcome_agent(config_store.DEFAULT_CONFIG)
        agent.update_instructions = boom
        asyncio.run(agent.handle_voice_welcome())  # 应用失败不许拖死欢迎语
        assert said == [config_store.DEFAULT_CONFIG.tts_voice]  # 保持旧配置音色


# ── duplex(全双工开关 2026-09-15):sanitize/load/save 纯 stdlib ─────────


class TestSessionDuplexConfig:
    def test_sanitize_accepts_legacy_duplex_but_does_not_persist_change(self):
        base = config_store.AgentConfig(persona_name="糖糖", persona_traits="x",
                                        tts_voice="longyan_v3", duplex_enabled=True)
        cfg, err = config_store.sanitize_config(None, None, None, base, duplex=False)
        assert err is None
        assert cfg.duplex_enabled is True
        assert cfg.persona_name == "糖糖" and cfg.tts_voice == "longyan_v3"

    def test_non_bool_rejects_whole_submission(self):
        """兼容旧脚本误传:非布尔("on"/1)整次拒绝,不许改一半。"""
        base = config_store.AgentConfig(persona_name="糖糖", persona_traits="x",
                                        tts_voice="longyan_v3")
        for bad in ("on", "off", 1, 0):
            cfg, err = config_store.sanitize_config("新名字", None, None, base, duplex=bad)
            assert err is not None and "duplex" in err, bad
            assert cfg == base  # 整次拒绝:连名字也不许改


class TestLoadSaveSessionDuplex:
    def test_save_omits_legacy_duplex_field_and_load_defaults_on(self, cfg_path: Path):
        cfg = config_store.AgentConfig(persona_name="糖糖", persona_traits="活泼",
                                        tts_voice="longyan_v3", duplex_enabled=False)
        config_store.save_agent_config(cfg)
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert "duplex_enabled" not in raw
        assert config_store.load_agent_config().duplex_enabled is True

    def test_missing_key_defaults_on(self, cfg_path: Path):
        """旧档案(2026-09-15 之前)无该 key → 默认开,存量行为零变化。"""
        cfg_path.write_text(json.dumps({
            "persona_name": "糖糖", "persona_traits": "活泼", "tts_voice": "longyan_v3"}),
            encoding="utf-8")
        assert config_store.load_agent_config().duplex_enabled is True

    def test_non_bool_in_file_defaults_on(self, cfg_path: Path):
        for bad in ("yes", None, 1):
            cfg_path.write_text(json.dumps({
                "persona_name": "糖糖", "persona_traits": "活泼",
                "tts_voice": "longyan_v3", "duplex_enabled": bad}), encoding="utf-8")
            config_store._cache = None  # 强读盘
            assert config_store.load_agent_config().duplex_enabled is True, bad

    def test_legacy_false_in_file_ignored_defaults_on(self, cfg_path: Path):
        config_store.save_agent_config(config_store.AgentConfig(
            persona_name="糖糖", persona_traits="活泼", tts_voice="longyan_v3"))
        assert config_store.load_agent_config().duplex_enabled is True
        cfg_path.write_text(json.dumps({
            "persona_name": "糖糖", "persona_traits": "活泼",
            "tts_voice": "longyan_v3", "duplex_enabled": False}), encoding="utf-8")
        os.utime(cfg_path, (1_800_000_000, 1_800_000_000))
        assert config_store.load_agent_config().duplex_enabled is True


@pytestmark_web
class TestHotApplySessionDuplex:
    """仅改持久配置不会改当前会话 duplex;会话态由 runtime 承载。"""

    def test_saved_legacy_duplex_off_is_ignored(self, cfg_path: Path, web_env: dict,
                                               monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(web_ui_agent.runtime, "duplex_enabled", True)
        cfg_path.write_text(json.dumps({
            "persona_name": "小歌",
            "persona_traits": config_store.DEFAULT_PERSONA_TRAITS,
            "tts_voice": config_store.DEFAULT_CONFIG.tts_voice,
            "duplex_enabled": False,
        }, ensure_ascii=False), encoding="utf-8")
        config_store._cache = None
        agent = _new_agent(config_store.DEFAULT_CONFIG)
        agent.update_instructions = _noop_update
        asyncio.run(agent._maybe_apply_agent_config())
        assert agent._agent_cfg.duplex_enabled is True
        assert web_ui_agent.runtime.duplex_enabled is True
        assert web_env["made"] == [] and web_env["switched"] == []  # 音色没变
        assert web_env["logs"] == []  # 旧 duplex=false 被忽略后与默认配置相等 → no-op

    def test_hot_apply_keeps_current_session_duplex_off(self, cfg_path: Path, web_env: dict,
                                                       monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(web_ui_agent.runtime, "duplex_enabled", False)
        config_store.save_agent_config(config_store.AgentConfig(persona_traits="更活泼"))
        agent = _new_agent(config_store.DEFAULT_CONFIG)
        agent.update_instructions = _noop_update
        asyncio.run(agent._maybe_apply_agent_config())
        assert agent._agent_cfg.persona_traits == "更活泼"
        assert web_ui_agent.runtime.duplex_enabled is False
        assert any("AGENT_CONFIG_APPLIED" in line and "duplex=off" in line
                   for line in web_env["logs"])


def _turn_agent(cfg):
    """on_user_turn_completed/handle_manual_text 绑定桩:真 _finalize(气泡经 broadcast)。"""
    agent = types.SimpleNamespace(_agent_cfg=cfg)
    agent.finalize_calls = []
    agent._wiring = types.SimpleNamespace(
        live=types.SimpleNamespace(
            finish_turn=lambda reason, utterance_id=None: "uid-t"))
    real_finalize = web_ui_agent.VoiceAgent._finalize_g3_user_message.__get__(agent)

    def finalize(original, enabled, utterance_id=None, reason="consumed_voice_turn"):
        agent.finalize_calls.append((original, enabled, reason))
        return real_finalize(original, enabled, utterance_id, reason)

    agent._finalize_g3_user_message = finalize

    async def noop_apply() -> None:
        return None

    agent._maybe_apply_agent_config = noop_apply
    agent._duplex_off = (
        web_ui_agent.VoiceAgent._duplex_off.__get__(agent)
    )
    agent._voice_welcome_said = False  # 2026-09-16 welcome unification: read by handle_voice_welcome
    return agent


def _duplex_off_cfg() -> "config_store.AgentConfig":
    return config_store.AgentConfig(
        persona_name="小歌",
        persona_traits=config_store.DEFAULT_PERSONA_TRAITS,
        tts_voice=config_store.DEFAULT_CONFIG.tts_voice,
        duplex_enabled=False)


@pytestmark_web
class TestDuplexOffGate:
    """语音轮 duplex 门:final 广播为 user 气泡 + StopResponse 吞轮 + 日志标记。"""

    def test_duplex_off_swallows_turn_with_user_bubble(
        self, cfg_path: Path, web_env: dict, monkeypatch: pytest.MonkeyPatch
    ):
        resets = []
        monkeypatch.setattr(web_ui_agent, "exit_reset_idle_timer",
                            lambda: resets.append(1))
        monkeypatch.setattr(web_ui_agent.runtime, "overlap_turn_state",
                            {"user_spoke_over_agent": False})
        monkeypatch.setattr(web_ui_agent.runtime, "duplex_enabled", False)
        broadcasts = []
        monkeypatch.setattr(web_ui_agent, "broadcast",
                            lambda msg: broadcasts.append(msg))
        agent = _turn_agent(_duplex_off_cfg())
        agent.on_user_turn_completed = (
            web_ui_agent.VoiceAgent.on_user_turn_completed.__get__(agent))
        msg = types.SimpleNamespace(text_content="今天天气怎么样",
                                    content=["今天天气怎么样"])
        with pytest.raises(web_ui_agent.StopResponse):
            asyncio.run(agent.on_user_turn_completed(object(), msg))
        # final 文本照常显示为 user 定稿气泡
        assert agent.finalize_calls == [("今天天气怎么样", True, "duplex_off")]
        assert any(m.get("role") == "user" and m.get("text") == "今天天气怎么样"
                   and m.get("type") == "message" for m in broadcasts)
        # 输入锚点先重臂(门序:reset 在 duplex 门之前)
        assert resets == [1]
        assert any("DUPLEX_OFF_TURN" in line and "今天天气怎么样" in line
                   for line in web_env["logs"])


@pytestmark_web
class TestManualTextDuplexOff:
    def test_manual_text_shows_bubble_no_reply(
        self, cfg_path: Path, web_env: dict, monkeypatch: pytest.MonkeyPatch
    ):
        resets = []
        monkeypatch.setattr(web_ui_agent, "exit_reset_idle_timer",
                            lambda: resets.append(1))
        broadcasts = []
        monkeypatch.setattr(web_ui_agent.runtime, "duplex_enabled", False)
        monkeypatch.setattr(web_ui_agent, "broadcast",
                            lambda msg: broadcasts.append(msg))
        generated = []

        agent = _turn_agent(_duplex_off_cfg())
        agent.session = types.SimpleNamespace(
            generate_reply=lambda **kw: generated.append(kw))
        agent.handle_manual_text = (
            web_ui_agent.VoiceAgent.handle_manual_text.__get__(agent))
        asyncio.run(agent.handle_manual_text("讲个笑话"))
        assert any(m.get("role") == "user" and m.get("text") == "讲个笑话"
                   and m.get("type") == "message" for m in broadcasts)
        assert generated == []  # 只显示不回复
        assert resets == [1]  # 文字也是输入,先重臂
        assert any("MANUAL_TEXT_SUPPRESSED duplex_off" in line
                   for line in web_env["logs"])


@pytestmark_web
class TestWelcomeDuplexOff:
    def test_voice_welcome_skipped(self, cfg_path: Path, web_env: dict,
                                   monkeypatch: pytest.MonkeyPatch):
        said = []
        monkeypatch.setattr(web_ui_agent.runtime, "duplex_enabled", False)
        monkeypatch.setattr(web_ui_agent, "say_voice_welcome",
                            lambda voice=None: said.append(voice))
        agent = _turn_agent(_duplex_off_cfg())
        agent.handle_voice_welcome = (
            web_ui_agent.VoiceAgent.handle_voice_welcome.__get__(agent))
        asyncio.run(agent.handle_voice_welcome())
        assert said == []  # ASR 测试模式要安静:零 TTS
        assert any("VOICE_WELCOME_SKIP duplex_off" in line for line in web_env["logs"])


# ── 真实 backends:音色优先级 显式参数 > agent_config.json > env ─────────

pytestmark_be = pytest.mark.skipif(
    real_backends is None or not hasattr(real_backends, "CosyVoiceStreamingTTS"),
    reason="real app.backends dependencies unavailable locally",
)


@pytest.fixture
def backends_env(cfg_path: Path, monkeypatch: pytest.MonkeyPatch) -> list:
    made = []

    class _FakeCosy:
        def __init__(self, *, model=None, voice=None, api_key=None, opts=None):
            made.append({"model": model, "voice": voice})

    monkeypatch.setattr(real_backends, "CosyVoiceStreamingTTS", _FakeCosy)
    monkeypatch.setenv("COSYVOICE_MODEL", "cosyvoice-v3-flash")
    monkeypatch.delenv("COSYVOICE_VOICE", raising=False)
    return made


@pytestmark_be
class TestBackendsVoicePriority:
    def test_file_overrides_env_when_no_explicit(self, backends_env: list):
        config_store.save_agent_config(config_store.AgentConfig(
            persona_name="小歌",
            persona_traits=config_store.DEFAULT_PERSONA_TRAITS,
            tts_voice="longyan_v3"))
        import os
        os.environ["COSYVOICE_VOICE"] = "longanli_v3"  # env 应输给文件
        try:
            real_backends._make_tts_cosyvoice()
        finally:
            del os.environ["COSYVOICE_VOICE"]
        assert backends_env == [{"model": "cosyvoice-v3-flash", "voice": "longyan_v3"}]

    def test_explicit_param_beats_file(self, backends_env: list):
        config_store.save_agent_config(config_store.AgentConfig(
            persona_name="小歌",
            persona_traits=config_store.DEFAULT_PERSONA_TRAITS,
            tts_voice="longyan_v3"))
        real_backends._make_tts_cosyvoice(voice="longanlang_v3")
        assert backends_env == [{"model": "cosyvoice-v3-flash", "voice": "longanlang_v3"}]

    def test_env_used_when_no_file_no_explicit(self, backends_env: list):
        import os
        os.environ["COSYVOICE_VOICE"] = "longanli_v3"
        try:
            real_backends.make_tts_backend("cosyvoice")
        finally:
            del os.environ["COSYVOICE_VOICE"]
        assert backends_env == [{"model": "cosyvoice-v3-flash", "voice": "longanli_v3"}]

    def test_corrupt_file_falls_back_to_env(self, backends_env: list):
        """损坏文件 = 没保存过(内部 None)→ env 接管,不是默认值挡 env。"""
        cfg_path = Path(config_store.AGENT_CONFIG_PATH)
        cfg_path.write_text("{not json", encoding="utf-8")
        config_store._cache = None  # 清 mtime 缓存,强读盘
        import os
        os.environ["COSYVOICE_VOICE"] = "longanli_v3"
        try:
            real_backends.make_tts_backend("cosyvoice")
        finally:
            del os.environ["COSYVOICE_VOICE"]
            cfg_path.unlink()
            config_store._cache = None
        assert backends_env == [{"model": "cosyvoice-v3-flash", "voice": "longanli_v3"}]


# ── 完整提示词(instructions_override;2026-09-17 llm-bak 上线,2026-09-20 移植 -97) ──
# None = 默认拼装(persona_instructions + FIXED_AGENT_INSTRUCTIONS,名字≠小歌时
# 固定段一并换名);有值 = 整段替换 system prompt。清除唯一通道 = 提交内容与
# 「按新人设拼出的默认」逐字相等(sanitize 存 None);空串一律拒绝。


class TestInstructionsCompose:
    def test_default_instructions_matches_tree_compose(self):
        """默认拼装长度哨兵，覆盖人设、设备控制与能力边界提示词。"""
        s = config_store.default_instructions(config_store.DEFAULT_CONFIG)
        assert s.startswith("# 你是谁\n你叫小歌，")
        assert "# 保持本色" in s and "# 身份与保密" in s
        assert "# 设备控制" in s  # -97 方案B 独有段(命令工具化行为规则)
        assert "up/down/mute/set 四档" in s  # volset 措辞在位
        assert "禁止自己编一个数值用 set" in s  # 反编造规则在位
        assert "判断“这句是不是在跟你说话”要果断" in s  # not_addressed 果断三条件(2026-10-12)
        assert len(s) == 3440

    def test_default_instructions_has_capability_boundaries(self):
        s = config_store.default_instructions(config_store.DEFAULT_CONFIG)
        assert "# 真实能力边界" in s
        assert "只能承诺这些真实能力" in s
        assert "查航班" in s and "订机票" in s and "查快递" in s
        assert "这个我现在还不能直接查/办理" in s
        assert "你会什么/能干什么" in s
        assert "充电状态/在充电吗→chg" not in s  # chg 已退出交互协议

    def test_default_instructions_name_substitution(self):
        cfg = config_store.AgentConfig(persona_name="糖糖", persona_traits="活泼")
        s = config_store.default_instructions(cfg)
        assert "小歌" not in s
        assert "以糖糖的身份" in s

    def test_effective_prefers_override(self):
        cfg = config_store.AgentConfig(instructions_override="# 全自定义\n就这么定了")
        s = config_store.effective_instructions(cfg)
        assert s.startswith("# 全自定义\n就这么定了")
        assert config_store.CAPABILITY_GUARD_INSTRUCTIONS in s  # 护栏不可覆盖
        assert "地图路线/导航" in s and "查电量" in s

    def test_effective_none_uses_default_compose_with_guard(self):
        s = config_store.effective_instructions(config_store.DEFAULT_CONFIG)
        assert s.startswith(config_store.default_instructions(config_store.DEFAULT_CONFIG))
        assert config_store.CAPABILITY_GUARD_INSTRUCTIONS in s


class TestSanitizeInstructions:
    def test_none_keeps_override(self):
        """部分提交语义:不带 instructions → 既有 override 原样保留。"""
        base = config_store.AgentConfig(instructions_override="旧自定义")
        cfg, err = config_store.sanitize_config(None, None, None, base)
        assert err is None
        assert cfg.instructions_override == "旧自定义"

    def test_set_override_strips(self):
        cfg, err = config_store.sanitize_config(
            None, None, None, config_store.DEFAULT_CONFIG,
            instructions="  # 新提示词 \n")
        assert err is None
        assert cfg.instructions_override == "# 新提示词"

    def test_empty_rejected(self):
        cfg, err = config_store.sanitize_config(
            None, None, None, config_store.DEFAULT_CONFIG, instructions="  \n ")
        assert err is not None and "empty" in err
        assert cfg == config_store.DEFAULT_CONFIG  # 整次拒绝

    def test_too_long_rejected(self):
        cfg, err = config_store.sanitize_config(
            None, None, None, config_store.DEFAULT_CONFIG,
            instructions="字" * (config_store.INSTRUCTIONS_MAX_CHARS + 1))
        assert err is not None and "too long" in err

    def test_non_string_rejected(self):
        cfg, err = config_store.sanitize_config(
            None, None, None, config_store.DEFAULT_CONFIG, instructions=123)
        assert err is not None and "instructions must be a string" in err

    def test_equals_default_stores_none(self):
        """「恢复默认」唯一通道:与默认拼装逐字相等 → 存 None(替换旧 override)。"""
        text = config_store.default_instructions(config_store.DEFAULT_CONFIG)
        base = config_store.AgentConfig(instructions_override="旧自定义")
        cfg, err = config_store.sanitize_config(None, None, None, base,
                                                instructions=text)
        assert err is None
        assert cfg.instructions_override is None

    def test_equals_new_default_after_persona_change(self):
        """同一笔提交连人设一起改:比对基准 = 按新人设拼出的默认。"""
        new_traits = "全新的性格"
        candidate_default = config_store.default_instructions(
            config_store.AgentConfig(persona_traits=new_traits))
        cfg, err = config_store.sanitize_config(
            None, new_traits, None, config_store.DEFAULT_CONFIG,
            instructions=candidate_default)
        assert err is None
        assert cfg.persona_traits == new_traits
        assert cfg.instructions_override is None  # 等于新默认 → 清除

    def test_old_default_text_becomes_override_when_persona_changed(self):
        """改名后提交改名前的默认全文 → 不等于新默认 → 存为普通自定义。"""
        old_default = config_store.default_instructions(config_store.DEFAULT_CONFIG)
        cfg, err = config_store.sanitize_config(
            "糖糖", None, None, config_store.DEFAULT_CONFIG,
            instructions=old_default)
        assert err is None
        assert cfg.persona_name == "糖糖"
        assert cfg.instructions_override == old_default


class TestLoadSaveInstructions:
    def test_save_override_roundtrip(self, cfg_path: Path):
        config_store.save_agent_config(config_store.AgentConfig(
            instructions_override="# 自定义提示词"))
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert raw["instructions_override"] == "# 自定义提示词"
        assert config_store.load_agent_config().instructions_override == "# 自定义提示词"

    def test_missing_key_loads_none(self, cfg_path: Path):
        """旧存档无 instructions_override key → None(默认拼装),不炸。"""
        cfg_path.write_text(json.dumps({
            "persona_name": "小歌",
            "persona_traits": config_store.DEFAULT_PERSONA_TRAITS,
            "tts_voice": "longxiaochun_v3",
            "duplex_enabled": True,
        }, ensure_ascii=False), encoding="utf-8")
        config_store._cache = None
        assert config_store.load_agent_config().instructions_override is None

    def test_null_field_loads_none(self, cfg_path: Path):
        cfg_path.write_text(json.dumps({
            "persona_name": "小歌",
            "persona_traits": config_store.DEFAULT_PERSONA_TRAITS,
            "tts_voice": "longxiaochun_v3",
            "instructions_override": None,
        }, ensure_ascii=False), encoding="utf-8")
        config_store._cache = None
        assert config_store.load_agent_config().instructions_override is None

    def test_stored_default_equal_text_normalizes_to_none(self, cfg_path: Path):
        """文件里存的 override 恰好等于默认全文 → load 走 sanitize → 归一成 None。"""
        text = config_store.default_instructions(config_store.DEFAULT_CONFIG)
        cfg_path.write_text(json.dumps({
            "persona_name": "小歌",
            "persona_traits": config_store.DEFAULT_PERSONA_TRAITS,
            "tts_voice": "longxiaochun_v3",
            "instructions_override": text,
        }, ensure_ascii=False), encoding="utf-8")
        config_store._cache = None
        assert config_store.load_agent_config().instructions_override is None


@pytestmark_web
class TestBuildInstructionsOverride:
    def test_override_keeps_guardrail(self):
        cfg = config_store.AgentConfig(
            persona_name="糖糖", persona_traits="活泼",
            instructions_override="# 整段替换")
        s = web_ui_agent._build_agent_instructions(cfg)
        assert s.startswith("# 整段替换")  # 人设块/固定段都不参与
        assert config_store.CAPABILITY_GUARD_INSTRUCTIONS in s
        assert "以糖糖的身份" not in s

    def test_none_falls_back_to_default_compose_with_guard(self):
        s = web_ui_agent._build_agent_instructions(config_store.DEFAULT_CONFIG)
        assert s.startswith(config_store.default_instructions(config_store.DEFAULT_CONFIG))
        assert config_store.CAPABILITY_GUARD_INSTRUCTIONS in s


@pytestmark_web
class TestHotApplyInstructions:
    def test_override_change_applies_new_instructions(self, cfg_path: Path,
                                                      web_env: dict):
        config_store.save_agent_config(config_store.AgentConfig(
            instructions_override="# 新自定义提示词"))
        agent = _new_agent(config_store.DEFAULT_CONFIG)
        applied = []

        async def collect(instructions: str) -> None:
            applied.append(instructions)

        agent.update_instructions = collect
        asyncio.run(agent._maybe_apply_agent_config())
        assert applied == [
            "# 新自定义提示词\n\n" + config_store.CAPABILITY_GUARD_INSTRUCTIONS
        ]
        assert agent._agent_cfg.instructions_override == "# 新自定义提示词"
        assert any("AGENT_CONFIG_APPLIED" in line and "instructions=custom" in line
                   for line in web_env["logs"])

    def test_clearing_override_back_to_default(self, cfg_path: Path, web_env: dict):
        """override → None:配置不等 → 重新下发默认拼装全文。"""
        config_store.save_agent_config(config_store.DEFAULT_CONFIG)
        agent = _new_agent(config_store.AgentConfig(
            instructions_override="# 旧自定义"))
        applied = []

        async def collect(instructions: str) -> None:
            applied.append(instructions)

        agent.update_instructions = collect
        asyncio.run(agent._maybe_apply_agent_config())
        expected = (config_store.default_instructions(config_store.DEFAULT_CONFIG)
                    + "\n\n" + config_store.CAPABILITY_GUARD_INSTRUCTIONS)
        assert applied == [expected]
        assert any("instructions=default" in line for line in web_env["logs"])


# ── 会话性格预设(2026-09-22,仅 -97) ─────────────────────────────────
# 三档固定 key(witty/sunny/gentle,第一档 witty=默认档,用户 2026-09-22 指定):
# 内容(标签/性格/音色)可编辑持久化,选择是会话态(runtime.session_persona,
# 新会话 attach 落回 DEFAULT_PERSONA_KEY)。POST 走部分合并 + 整次全拒;
# 读档 lenient 逐字段回落,但合法空串音色(跟随全局)必须原样保留。


class TestSanitizePersonas:
    def test_default_shape(self):
        assert config_store.PERSONA_KEYS == ("witty", "sunny", "gentle")
        assert config_store.DEFAULT_PERSONA_KEY == "witty"
        assert config_store.DEFAULT_PERSONA_KEY == config_store.PERSONA_KEYS[0]
        assert set(config_store.DEFAULT_CONFIG.personas) == set(config_store.PERSONA_KEYS)
        for key in config_store.PERSONA_KEYS:
            entry = config_store.DEFAULT_CONFIG.personas[key]
            assert entry["label"] == config_store.DEFAULT_PERSONAS[key]["label"]
            assert entry["traits"] == config_store.DEFAULT_PERSONAS[key]["traits"]
            assert entry["tts_voice"] == config_store.DEFAULT_PERSONAS[key]["tts_voice"]

    def test_partial_merge_keeps_rest(self):
        """部分合并语义:只改 sunny.label,其余字段/其余两档原样保留。"""
        base = config_store.DEFAULT_CONFIG
        cfg, err = config_store.sanitize_config(
            None, None, None, base,
            personas={"sunny": {"label": "小太阳"}})
        assert err is None
        assert cfg.personas["sunny"]["label"] == "小太阳"
        assert cfg.personas["sunny"]["traits"] == base.personas["sunny"]["traits"]
        assert cfg.personas["sunny"]["tts_voice"] == base.personas["sunny"]["tts_voice"]
        assert cfg.personas["gentle"] == base.personas["gentle"]
        assert cfg.personas["witty"] == base.personas["witty"]

    def test_none_and_empty_keeps_base(self):
        """不带 personas(None)/空对象 = 不改预设(与其它字段的部分提交语义一致)。"""
        for absent in (None, {}):
            cfg, err = config_store.sanitize_config(
                None, None, None, config_store.DEFAULT_CONFIG, personas=absent)
            assert err is None
            assert cfg.personas == config_store.DEFAULT_CONFIG.personas

    def test_unknown_key_rejects_whole(self):
        """键固定三档:试图加第四档 → 整次拒绝(不写半截)。"""
        cfg, err = config_store.sanitize_config(
            None, None, None, config_store.DEFAULT_CONFIG,
            personas={"nope": {"label": "x"}})
        assert err is not None and "unknown persona key" in err
        assert cfg == config_store.DEFAULT_CONFIG

    def test_unknown_field_rejects_whole(self):
        cfg, err = config_store.sanitize_config(
            None, None, None, config_store.DEFAULT_CONFIG,
            personas={"sunny": {"bad": "x"}})
        assert err is not None and "not editable" in err
        assert cfg == config_store.DEFAULT_CONFIG

    def test_invalid_voice_rejects_whole(self):
        cfg, err = config_store.sanitize_config(
            None, None, None, config_store.DEFAULT_CONFIG,
            personas={"sunny": {"tts_voice": "no-such-voice"}})
        assert err is not None and "voice" in err
        assert cfg == config_store.DEFAULT_CONFIG

    def test_empty_voice_means_follow_global(self):
        """空串音色 = 跟随全局:合法存值,不许被拒或回填默认。"""
        cfg, err = config_store.sanitize_config(
            None, None, None, config_store.DEFAULT_CONFIG,
            personas={"sunny": {"tts_voice": " "}})
        assert err is None
        assert cfg.personas["sunny"]["tts_voice"] == ""

    def test_empty_label_and_traits_rejected(self):
        for fname in ("label", "traits"):
            cfg, err = config_store.sanitize_config(
                None, None, None, config_store.DEFAULT_CONFIG,
                personas={"sunny": {fname: "  \n "}})
            assert err is not None and fname in err, fname
            assert cfg == config_store.DEFAULT_CONFIG

    def test_too_long_rejected(self):
        cfg, err = config_store.sanitize_config(
            None, None, None, config_store.DEFAULT_CONFIG,
            personas={"sunny": {"label": "名" * (config_store.PERSONA_NAME_MAX_CHARS + 1)}})
        assert err is not None and "too long" in err
        cfg, err = config_store.sanitize_config(
            None, None, None, config_store.DEFAULT_CONFIG,
            personas={"gentle": {"traits": "长" * (config_store.PERSONA_TRAITS_MAX_CHARS + 1)}})
        assert err is not None and "too long" in err

    def test_non_string_and_non_object_rejected(self):
        for bad in ({"sunny": {"label": 123}},
                    {"sunny": "not-an-object"},
                    ["sunny"]):
            cfg, err = config_store.sanitize_config(
                None, None, None, config_store.DEFAULT_CONFIG, personas=bad)
            assert err is not None, bad
            assert cfg == config_store.DEFAULT_CONFIG

    def test_values_are_stripped(self):
        cfg, err = config_store.sanitize_config(
            None, None, None, config_store.DEFAULT_CONFIG,
            personas={"sunny": {"label": "  小太阳  ", "traits": "  元气加倍  "}})
        assert err is None
        assert cfg.personas["sunny"]["label"] == "小太阳"
        assert cfg.personas["sunny"]["traits"] == "元气加倍"


class TestLoadSavePersonas:
    def test_roundtrip_custom_personas(self, cfg_path: Path):
        """POST 合并后的形状(三档齐全)落盘 → 读档逐字一致,存档自包含。"""
        personas = {"sunny": {"label": "小太阳", "traits": "元气加倍",
                              "tts_voice": "longanlang_v3"}}
        personas.update({k: dict(v) for k, v in
                         config_store.DEFAULT_PERSONAS.items() if k != "sunny"})
        cfg = config_store.replace(config_store.DEFAULT_CONFIG, personas=personas)
        config_store.save_agent_config(cfg)
        assert config_store.load_agent_config() == cfg
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert raw["personas"]["sunny"]["label"] == "小太阳"
        assert raw["personas"]["sunny"]["tts_voice"] == "longanlang_v3"
        # 未编辑的两档照默认写全(存档自包含,不依赖代码默认表)
        assert raw["personas"]["gentle"] == config_store.DEFAULT_PERSONAS["gentle"]

    def test_partial_dict_fills_on_load(self, cfg_path: Path):
        """内存里的部分 personas(生产三路=default_factory/sanitize/load 均全档,
        仅测试可构造)落盘如实只写已有键,读档 lenient 补全三档——脏存档不拖垮配置。"""
        cfg = config_store.AgentConfig(
            personas={"sunny": {"label": "小太阳", "traits": "元气加倍",
                                "tts_voice": ""}})
        config_store.save_agent_config(cfg)
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert set(raw["personas"]) == {"sunny"}            # 落盘不偷偷补
        loaded = config_store.load_agent_config()
        assert loaded.personas["sunny"]["tts_voice"] == ""  # 空串=跟随全局,保留
        assert loaded.personas["gentle"] == config_store.DEFAULT_PERSONAS["gentle"]
        assert loaded.personas["witty"] == config_store.DEFAULT_PERSONAS["witty"]

    def test_missing_key_loads_defaults(self, cfg_path: Path):
        """旧存档(Sep-22 前)无 personas key → 默认表,存量行为零变化。"""
        cfg_path.write_text(json.dumps({
            "persona_name": "糖糖", "persona_traits": "活泼", "tts_voice": "longyan_v3"}),
            encoding="utf-8")
        assert config_store.load_agent_config().personas == config_store.DEFAULT_PERSONAS

    def test_empty_voice_in_file_preserved(self, cfg_path: Path):
        """合法空串音色(跟随全局)读档不许被回填成默认——回填=改档。"""
        cfg_path.write_text(json.dumps({
            "persona_name": "小歌",
            "persona_traits": config_store.DEFAULT_PERSONA_TRAITS,
            "tts_voice": "longxiaochun_v3",
            "personas": {"sunny": {"label": "阳光元气",
                                   "traits": "自定义元气", "tts_voice": ""}},
        }, ensure_ascii=False), encoding="utf-8")
        config_store._cache = None
        loaded = config_store.load_agent_config()
        assert loaded.personas["sunny"]["tts_voice"] == ""
        assert loaded.personas["sunny"]["traits"] == "自定义元气"

    def test_garbage_falls_back_per_field(self, cfg_path: Path):
        """存档 lenient:单键/单字段坏只回落该处,其余照读(整档不炸)。"""
        cfg_path.write_text(json.dumps({
            "persona_name": "小歌",
            "persona_traits": config_store.DEFAULT_PERSONA_TRAITS,
            "tts_voice": "longxiaochun_v3",
            "personas": {
                "sunny": "not-an-object",  # 整条坏 → 默认
                "gentle": {"label": 123, "traits": "温柔的", "tts_voice": None},
                "witty": {"label": "冷面幽默", "traits": "照读",
                          "tts_voice": "bad-voice"},
                "nope": {"label": "多出的键"},  # 非 PERSONA_KEYS → 丢弃
            },
        }, ensure_ascii=False), encoding="utf-8")
        config_store._cache = None
        loaded = config_store.load_agent_config()
        assert loaded.personas["sunny"] == config_store.DEFAULT_PERSONAS["sunny"]
        assert loaded.personas["gentle"]["label"] == config_store.DEFAULT_PERSONAS["gentle"]["label"]
        assert loaded.personas["gentle"]["traits"] == "温柔的"
        assert loaded.personas["gentle"]["tts_voice"] == config_store.DEFAULT_PERSONAS["gentle"]["tts_voice"]
        assert loaded.personas["witty"]["traits"] == "照读"
        assert loaded.personas["witty"]["tts_voice"] == config_store.DEFAULT_PERSONAS["witty"]["tts_voice"]
        assert set(loaded.personas) == set(config_store.PERSONA_KEYS)

    def test_non_dict_personas_in_file_defaults(self, cfg_path: Path):
        cfg_path.write_text(json.dumps({
            "persona_name": "小歌",
            "persona_traits": config_store.DEFAULT_PERSONA_TRAITS,
            "tts_voice": "longxiaochun_v3",
            "personas": ["sunny"],
        }, ensure_ascii=False), encoding="utf-8")
        config_store._cache = None
        assert config_store.load_agent_config().personas == config_store.DEFAULT_PERSONAS


class TestPersonasPublic:
    def test_order_and_shape(self):
        """GET 目录:固定三档顺序(第一档=默认档),键全给(面板下拉+编辑弹框共用)。"""
        out = config_store.personas_public(config_store.DEFAULT_CONFIG)
        assert [p["key"] for p in out] == ["witty", "sunny", "gentle"]
        for p in out:
            assert set(p) == {"key", "label", "traits", "tts_voice"}
            assert p["label"] and p["traits"]

    def test_missing_entry_falls_back_per_key(self):
        """空表/缺档 → 逐档回落默认,目录恒三档(面板不遇空下拉)。"""
        out = config_store.personas_public(config_store.AgentConfig(personas={}))
        assert [p["key"] for p in out] == ["witty", "sunny", "gentle"]
        assert out[0] == {"key": "witty", **config_store.DEFAULT_PERSONAS["witty"]}

    def test_reflects_edits(self):
        cfg = config_store.AgentConfig(
            personas={"sunny": {"label": "小太阳", "traits": "元气加倍",
                                "tts_voice": ""}})
        out = config_store.personas_public(cfg)
        sunny = [p for p in out if p["key"] == "sunny"][0]
        assert sunny["label"] == "小太阳"
        assert sunny["tts_voice"] == ""  # 空串照给(编辑弹框回填「跟随全局」)


@pytestmark_web
class TestHotApplyPersona:
    """轮次起点按 runtime.session_persona 算生效配置(存 key 热引用)。"""

    def _persona_agent(self, monkeypatch: pytest.MonkeyPatch, key,
                       explicit: bool = True):
        monkeypatch.setattr(web_ui_agent.runtime, "session_persona", key)
        # explicit 默认 True:这些用例都模拟"会话内手动选档"(v1 语义);
        # attach 落默认档的用例显式传 False。
        monkeypatch.setattr(web_ui_agent.runtime,
                            "session_persona_explicit", explicit)
        return _new_agent(config_store.DEFAULT_CONFIG)

    def test_persona_overrides_traits_but_keeps_global_voice(
            self, cfg_path: Path, web_env: dict, monkeypatch: pytest.MonkeyPatch):
        agent = self._persona_agent(monkeypatch, "sunny")
        applied = []

        async def collect(instructions: str) -> None:
            applied.append(instructions)

        agent.update_instructions = collect
        asyncio.run(agent._maybe_apply_agent_config())
        sunny = config_store.DEFAULT_PERSONAS["sunny"]
        assert len(applied) == 1
        assert sunny["traits"].split("，")[0] in applied[0]  # 预设性格块生效
        assert web_env["made"] == []  # 性格不切换音色
        assert web_env["switched"] == []
        assert agent._agent_cfg.tts_voice == config_store.DEFAULT_CONFIG.tts_voice
        assert agent._agent_cfg.persona_traits == sunny["traits"]
        assert any("AGENT_CONFIG_APPLIED" in line and "persona=sunny" in line
                   for line in web_env["logs"])

    def test_persona_empty_voice_follows_global(
            self, cfg_path: Path, web_env: dict, monkeypatch: pytest.MonkeyPatch):
        """预设音色空串 = 跟随全局:只换性格不换 TTS。"""
        config_store.save_agent_config(config_store.AgentConfig(
            personas={"sunny": {"label": "阳光元气", "traits": "元气加倍",
                                "tts_voice": ""}}))
        agent = self._persona_agent(monkeypatch, "sunny")
        agent.update_instructions = _noop_update
        asyncio.run(agent._maybe_apply_agent_config())
        assert web_env["made"] == [] and web_env["switched"] == []
        assert agent._agent_cfg.tts_voice == config_store.DEFAULT_CONFIG.tts_voice
        assert agent._agent_cfg.persona_traits == "元气加倍"

    def test_persona_beats_instructions_override(
            self, cfg_path: Path, web_env: dict, monkeypatch: pytest.MonkeyPatch):
        """会话内显式选性格(explicit=True)> 全局自定义完整提示词(override 让位)。"""
        config_store.save_agent_config(config_store.AgentConfig(
            instructions_override="# 全自定义"))
        agent = self._persona_agent(monkeypatch, "gentle")
        agent.update_instructions = _noop_update
        asyncio.run(agent._maybe_apply_agent_config())
        assert agent._agent_cfg.instructions_override is None
        assert agent._agent_cfg.persona_traits == (
            config_store.DEFAULT_PERSONAS["gentle"]["traits"])
        assert any("instructions=default" in line and "persona=gentle" in line
                   for line in web_env["logs"])

    def test_attach_default_persona_keeps_override(
            self, cfg_path: Path, web_env: dict, monkeypatch: pytest.MonkeyPatch):
        """attach 落的默认档非显式:不 mask 全局自定义完整提示词。

        2026-09-22 fullprompt 回归:三档化后新会话恒落 witty,默认档若也 mask,
        每个新会话的自定义完整提示词都失效。非显式档仍带 traits/音色(音色照换,
        override 原样进 prompt；全局音色保持不变）。
        """
        config_store.save_agent_config(config_store.AgentConfig(
            instructions_override="# 全自定义"))
        agent = self._persona_agent(monkeypatch, "witty", explicit=False)
        agent.update_instructions = _noop_update
        asyncio.run(agent._maybe_apply_agent_config())
        assert agent._agent_cfg.instructions_override == "# 全自定义"
        assert agent._agent_cfg.persona_traits == (
            config_store.DEFAULT_PERSONAS["witty"]["traits"])
        assert agent._agent_cfg.tts_voice == config_store.DEFAULT_CONFIG.tts_voice
        assert any("instructions=custom" in line and "persona=witty" in line
                   for line in web_env["logs"])

    def test_unknown_key_falls_back_to_global(
            self, cfg_path: Path, web_env: dict, monkeypatch: pytest.MonkeyPatch):
        """老配置缺键/坏 key → 回落全局人设,不许炸(配置热更不打断对话)。"""
        agent = self._persona_agent(monkeypatch, "nope")
        agent.update_instructions = _noop_update
        asyncio.run(agent._maybe_apply_agent_config())
        assert web_env["logs"] == []  # 生效=默认 → no-op
        assert agent._agent_cfg == config_store.DEFAULT_CONFIG

    def test_switch_back_to_default_reapplies(
            self, cfg_path: Path, web_env: dict, monkeypatch: pytest.MonkeyPatch):
        """档位切回默认:生效配置回全局,音色保持全局设置。"""
        agent = _new_agent(web_ui_agent.VoiceAgent._effective_agent_config(
            config_store.DEFAULT_CONFIG, "sunny"))
        monkeypatch.setattr(web_ui_agent.runtime, "session_persona", None)
        agent.update_instructions = _noop_update
        asyncio.run(agent._maybe_apply_agent_config())
        assert agent._agent_cfg == config_store.DEFAULT_CONFIG
        assert web_env["made"] == []
        assert web_env["switched"] == []
        assert any("persona=none" in line for line in web_env["logs"])

    def test_persona_key_is_hot_reference(
            self, cfg_path: Path, web_env: dict, monkeypatch: pytest.MonkeyPatch):
        """存 key 不存快照:预设内容 POST 改档后,下一轮取到新文。"""
        config_store.save_agent_config(config_store.AgentConfig(
            personas={"sunny": {"label": "阳光元气", "traits": "第一版",
                                "tts_voice": ""}}))
        agent = self._persona_agent(monkeypatch, "sunny")
        applied = []

        async def collect(instructions: str) -> None:
            applied.append(instructions)

        agent.update_instructions = collect
        asyncio.run(agent._maybe_apply_agent_config())
        assert "第一版" in applied[0]

        config_store.save_agent_config(config_store.AgentConfig(
            personas={"sunny": {"label": "阳光元气", "traits": "第二版",
                                "tts_voice": ""}}))
        os.utime(cfg_path, (1_800_000_000, 1_800_000_000))  # 强制 mtime 变化
        asyncio.run(agent._maybe_apply_agent_config())
        assert len(applied) == 2
        assert "第二版" in applied[1] and "第一版" not in applied[1]
