from __future__ import annotations

"""天气查询工具(云侧, Open-Meteo)单测 — 2026-09-09 新增 query_weather。

不真连外网: monkeypatch web_ui_agent._weather_fetch_json 喂罐头 payload。
覆盖: WMO 文案表 / 事实卡格式化 / 默认城市(.env 坐标直行, 不走 geocode) /
外地城市 geocode 人口择优 / 10 分钟缓存 / 失败与未配置文案(不给编数字留口子)。
"""

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "voice_agents"))

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
sys.modules.setdefault("app.backends", backends)
providers_config = types.ModuleType("providers.config")
providers_config.funasr_hotwords = lambda: {}
sys.modules.setdefault("providers.config", providers_config)
silero = types.ModuleType("livekit.plugins.silero")
silero.VAD = types.SimpleNamespace(load=lambda *args, **kwargs: None)
sys.modules.setdefault("livekit.plugins.silero", silero)

import web_ui_agent  # noqa: E402


FAKE_FORECAST = {
    "current": {
        "time": "2026-09-09T22:00",
        "temperature_2m": 24.9,
        "weather_code": 2,
        "relative_humidity_2m": 36,
        "wind_speed_10m": 19.1,
    },
    "daily": {
        "time": ["2026-09-09", "2026-09-10", "2026-09-11"],
        "weather_code": [2, 0, 61],
        "temperature_2m_max": [28.1, 27.3, 25.0],
        "temperature_2m_min": [21.0, 20.2, 19.4],
        "precipitation_probability_max": [10, 0, 70],
    },
}
FAKE_GEO_BJ = {
    "results": [
        {"name": "北京", "latitude": 39.9042, "longitude": 116.4074,
         "country_code": "CN", "admin1": "北京市", "population": 21700000},
        # 同名小地方: 人口择优必须让位给大城市
        {"name": "北京", "latitude": 40.1, "longitude": 116.5,
         "country_code": "CN", "admin1": "河北省", "population": 90000},
    ]
}


@pytest.fixture
def weather_env(monkeypatch: pytest.MonkeyPatch) -> dict:
    """默认城市配置 + 清空模块级缓存(测试间互不污染) + 假 HTTP。"""
    monkeypatch.setenv("XIAOGE_WEATHER_CITY", "青岛")
    monkeypatch.setenv("XIAOGE_WEATHER_LAT", "36.09")
    monkeypatch.setenv("XIAOGE_WEATHER_LON", "120.38")
    monkeypatch.setattr(web_ui_agent, "_WEATHER_CACHE", {})
    monkeypatch.setattr(web_ui_agent, "_WEATHER_GEO_CACHE", {})
    calls = {"n": 0, "urls": []}

    async def fake_fetch(url: str, params: dict) -> dict | None:
        calls["n"] += 1
        calls["urls"].append(url)
        if "geocoding" in url:
            return FAKE_GEO_BJ
        return FAKE_FORECAST

    monkeypatch.setattr(web_ui_agent, "_weather_fetch_json", fake_fetch)
    return calls


async def _call_query_weather(city: str, session: object = None) -> str:
    """FunctionTool 是描述符: __get__(agent) 拿绑定版, 直接调原始协程。

    session=None 时桩上没有 session 属性(等价"session 没准备好"),
    传 mock 对象则记录 say 调用。
    """
    agent = types.SimpleNamespace(session=session)
    tool = web_ui_agent.VoiceAgent.query_weather.__get__(agent)
    return await tool(None, city)


@pytest.mark.parametrize(
    ("code", "want"),
    [(0, "晴"), (2, "多云"), (61, "小雨"), (95, "雷雨"), (999, "天气代码999")],
)
def test_wmo_text(code: int, want: str) -> None:
    assert web_ui_agent._wmo_text(code) == want


def test_wmo_text_bad_input() -> None:
    assert web_ui_agent._wmo_text(None) == "未知天气"
    assert web_ui_agent._wmo_text("abc") == "未知天气"


def test_format_weather_card() -> None:
    txt = web_ui_agent._format_weather(FAKE_FORECAST, "青岛")
    assert txt.startswith("青岛天气: 现在24.9°C，多云")
    assert "湿度36%" in txt and "风速19.1km/h" in txt
    assert "今天，多云，21.0~28.1°C，降雨概率10%" in txt
    assert "明天，晴，20.2~27.3°C" in txt  # 降雨概率 0 不报
    assert "后天，小雨，19.4~25.0°C，降雨概率70%" in txt
    assert txt.endswith("。")
    # 空 payload 不炸
    assert web_ui_agent._format_weather({}, "x") == "x天气:。"


async def test_query_weather_default_city(weather_env: dict) -> None:
    """没说城市 → .env 固定坐标, 只发一次 forecast, 不走 geocode(同名地名坑)。"""
    out = await _call_query_weather("")
    assert "青岛天气: 现在24.9°C" in out
    assert "口语" in out  # 给 LLM 的口径指引必须在
    assert weather_env["n"] == 1
    assert all("geocoding" not in u for u in weather_env["urls"])


async def test_query_weather_default_city_by_name(weather_env: dict) -> None:
    """说的就是默认城市名("青岛") → 也走固定坐标, 避开地理编码同名歧义。"""
    out = await _call_query_weather("青岛")
    assert "青岛天气: 现在24.9°C" in out
    assert all("geocoding" not in u for u in weather_env["urls"])


async def test_query_weather_named_city_geocode(weather_env: dict) -> None:
    """外地城市 → geocode + forecast 两次请求, 人口择优挑大城市。"""
    out = await _call_query_weather("北京")
    assert "北京(北京市)天气: 现在24.9°C" in out
    assert weather_env["n"] == 2


async def test_query_weather_geocode_cached(
    weather_env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一外地城市第二查: geocode 结果缓存, 只再发 forecast。
    (要清掉天气缓存才能看到 geocode 缓存的效果, 否则两段缓存都命中。)"""
    await _call_query_weather("北京")  # geocode + forecast
    web_ui_agent._WEATHER_CACHE.clear()  # 只留 geocode 缓存
    await _call_query_weather("北京")  # 应只再发 forecast, 不再 geocode
    assert weather_env["n"] == 3  # geocode 1 + forecast 2


async def test_query_weather_cache_hit(weather_env: dict) -> None:
    """同一城 10 分钟内第二查直接用缓存, 不再发任何请求, 且口径指引不丢。"""
    await _call_query_weather("")
    n_after_first = weather_env["n"]
    out = await _call_query_weather("")
    assert weather_env["n"] == n_after_first
    assert "青岛天气: 现在24.9°C" in out
    assert "口语" in out


async def test_query_weather_fetch_fail(
    weather_env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """网络/服务挂 → '暂时失败', 明确告诉 LLM 别编数字。"""

    async def dead_fetch(url: str, params: dict) -> dict | None:
        return None

    monkeypatch.setattr(web_ui_agent, "_weather_fetch_json", dead_fetch)
    out = await _call_query_weather("")
    assert "暂时失败" in out
    assert "不要编" in out


async def test_query_weather_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    """没配默认城市又没说城市 → '没查到这个城市', 不抛异常。"""
    monkeypatch.delenv("XIAOGE_WEATHER_CITY", raising=False)
    monkeypatch.delenv("XIAOGE_WEATHER_LAT", raising=False)
    monkeypatch.delenv("XIAOGE_WEATHER_LON", raising=False)
    monkeypatch.setattr(web_ui_agent, "_WEATHER_CACHE", {})
    monkeypatch.setattr(web_ui_agent, "_WEATHER_GEO_CACHE", {})

    async def dead_fetch(url: str, params: dict) -> dict | None:
        return None

    monkeypatch.setattr(web_ui_agent, "_weather_fetch_json", dead_fetch)
    out = await _call_query_weather("")
    assert "没查到这个城市" in out


async def test_query_weather_unknown_city(
    weather_env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """geocode 无结果(乱码城市名) → '没查到这个城市'。"""

    async def fake_fetch_empty_geo(url: str, params: dict) -> dict | None:
        if "geocoding" in url:
            return {"results": []}
        return FAKE_FORECAST

    monkeypatch.setattr(web_ui_agent, "_weather_fetch_json", fake_fetch_empty_geo)
    out = await _call_query_weather("不存在的城市xyz")
    assert "没查到这个城市" in out


async def test_query_weather_ack_says_guide_word(weather_env: dict) -> None:
    """2026-09-10 工具一被调先说引导词垫感知延迟: 命中三选一、不进聊天
    上下文、不可打断; 说完查询照常出结果。"""
    said: list[tuple[str, dict]] = []

    class _Sess:
        def say(self, text: str, **kw: object) -> None:
            said.append((text, kw))

    out = await _call_query_weather("", session=_Sess())
    assert "青岛天气: 现在24.9°C" in out
    assert said == []


async def test_query_weather_ack_without_session(weather_env: dict) -> None:
    """session 缺席(极早期/桩)时引导词静默跳过, 查询不断链。"""
    out = await _call_query_weather("")  # 桩上 session=None
    assert "青岛天气: 现在24.9°C" in out
