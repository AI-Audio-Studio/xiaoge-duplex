"""WebPanelState:Web 控制面板(独立线程 + 独立事件循环)的共享状态与配置。

跨循环纪律(与拆分前一致):
  - agent→web:bridge.broadcast* 用 run_coroutine_threadsafe(..., panel.web_loop)
  - web→agent:经 runtime.agent_loop 的 call_soon_threadsafe / run_coroutine_threadsafe
  - 绝不在 web 处理器里直接 await agent 侧协程
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from common.config_utils import env_bool

if TYPE_CHECKING:
    import aiohttp.web

WEB_PORT = int(os.getenv("WEB_UI_PORT", "8787"))  # 本地直启/测试统一 8787(D-23)
WEB_HOST = os.getenv("WEB_UI_HOST", "localhost")
WEB_AUDIO: bool = env_bool("WEB_AUDIO", False)
# M5 / D-19:asr/tts 管理路由(后端热切换)开关。**代码默认=显示(PC/测试形态不变)**;服务器形态
# 由池管理器注入 `XIAOGE_ADMIN_ROUTES=0` 隐藏——关时不注册即 404、tab 也不注入。/api/mic 不受约束。
ADMIN_ROUTES: bool = env_bool("XIAOGE_ADMIN_ROUTES", True)
SSL_CERT: str = os.getenv("WEB_SSL_CERT", "")
SSL_KEY: str = os.getenv("WEB_SSL_KEY", "")

BUSY_MESSAGE = "服务器繁忙，请稍后再试！"

BUSY_HTML = f"""\
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{BUSY_MESSAGE}</title>
<style>
html,body{{height:100%;margin:0}}
body{{display:flex;align-items:center;justify-content:center;background:#fff7f3;
color:#9a3412;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
.busy{{font-size:42px;font-weight:800;line-height:1.3;text-align:center;padding:32px}}
@media (max-width:640px){{.busy{{font-size:30px}}}}
</style>
</head>
<body><main class="busy">{BUSY_MESSAGE}</main></body>
</html>
"""


@dataclass
class WebPanelState:
    web_loop: asyncio.AbstractEventLoop | None = None
    connection_lock: asyncio.Lock | None = None
    ws_clients: set[aiohttp.web.WebSocketResponse] = field(default_factory=set)
    ws_primary_client: aiohttp.web.WebSocketResponse | None = None
    audio_ws_clients: set[aiohttp.web.WebSocketResponse] = field(default_factory=set)
    audio_ws_primary_client: aiohttp.web.WebSocketResponse | None = None


panel = WebPanelState()
