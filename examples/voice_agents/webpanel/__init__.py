"""小歌 Web 控制面板(阶段3,由 web_ui_agent.py 拆出)。

state    WebPanelState:web 线程侧状态(循环/客户端集合/端口/TLS 配置)
bridge   跨循环广播纪律(agent→web 走 run_coroutine_threadsafe)
server   aiohttp 路由/处理器/独立线程启动;页面在 static/index.html
"""
