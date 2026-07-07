"""行为锁定测试:并发改造 M5 / D-19——asr/tts 管理路由服务器形态隐藏(隐藏态 404)。

§12.2 checklist 第 16 条:asr/tts 隐藏 ≠ 仅前端无 tab,而是路由**根本不注册**、命中即 404。
**代码默认=显示(PC/测试形态不变)**;服务器由池管理器注入 `XIAOGE_ADMIN_ROUTES=0` 隐藏。
/api/mic 属产品功能不受约束。用真 aiohttp TestClient 打 `build_web_app` 真 app(路由决策为
被测点),不依赖云/模型。

同批见 `test_ours_concurrency_b_manager::test_env_injection_table`:池 `default_agent_env` 注入
`XIAOGE_ADMIN_ROUTES=0`(服务器形态隐藏)。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

_AGENT_DIR = Path(__file__).resolve().parents[1] / "examples" / "voice_agents"
sys.path.insert(0, str(_AGENT_DIR))

from webpanel import server as ws  # noqa: E402


def _paths(admin_routes: bool) -> set[str]:
    app = ws.build_web_app(admin_routes=admin_routes, web_audio=False)
    return {r.resource.canonical for r in app.router.routes() if r.resource is not None}


def test_admin_routes_hidden_when_gated_off() -> None:
    """门控关(服务器形态):/api/asr、/api/tts 不在路由集;/api/mic 与基础路由仍在。"""
    paths = _paths(admin_routes=False)
    assert "/api/asr" not in paths and "/api/tts" not in paths  # M5:隐藏 → 未注册
    assert "/api/mic" in paths  # 产品功能不受约束
    assert {"/", "/healthz", "/ws"} <= paths


def test_admin_routes_present_when_gated_on() -> None:
    """门控开(本地默认 / XIAOGE_ADMIN_ROUTES=1):asr/tts 注册可用。"""
    paths = _paths(admin_routes=True)
    assert "/api/asr" in paths and "/api/tts" in paths


def test_code_default_shows_admin_routes() -> None:
    """代码默认=显示(PC/测试形态不变):无参 build_web_app 保留 asr/tts。

    池管理器服务器形态注入 XIAOGE_ADMIN_ROUTES=0 才隐藏(见 b_manager env 注入表测)。"""
    if not ws.ADMIN_ROUTES:  # 测试环境若显式设了隐藏,跳过默认断言(避免误红)
        return
    app = ws.build_web_app(web_audio=False)  # 用模块默认(ADMIN_ROUTES)
    paths = {r.resource.canonical for r in app.router.routes() if r.resource is not None}
    assert "/api/asr" in paths and "/api/tts" in paths


def test_tab_injection_gated_by_admin_routes() -> None:
    """T1 tab 半:门控关时页面**不注入**后端切换 tab(开关同控路由 + tab);开时注入。"""
    tabs = ws.backend_tabs_html()
    assert tabs.strip()  # 注册表非空,tab 片段有内容(否则本测无意义)
    shown = ws._build_index_html(admin_routes=True)
    hidden = ws._build_index_html(admin_routes=False)
    assert tabs in shown  # 开:注入
    assert tabs not in hidden  # 关:不注入(T1 tab 同控)
    assert "<!--BACKEND_TABS-->" not in shown  # 占位已替换(非残留)
    assert "<!--BACKEND_TABS-->" not in hidden


def test_hidden_admin_route_returns_404_over_http() -> None:
    """端到端:隐藏态经真 HTTP 打 /api/asr、/api/tts → 404(而非仅前端无入口)。"""

    async def _main() -> None:
        app = ws.build_web_app(admin_routes=False, web_audio=False)
        async with TestClient(TestServer(app)) as cli:
            async with cli.post("/api/asr", json={"backend": "x"}) as r:
                assert r.status == 404
            async with cli.post("/api/tts", json={"backend": "x"}) as r:
                assert r.status == 404

    asyncio.run(_main())
