"""小歌应用装配层(阶段3,由 web_ui_agent.py 拆出)。

session_state   AppRuntime:agent 侧共享状态(原 20+ 模块级全局收敛于此)
switchable      SwitchableSTT/SwitchableTTS 热切换代理
backends        STT/TTS 后端注册表 + 工厂 + build_llm(扩展点单一来源)
listening_host  聆听模式 host 助手(agent 循环线程执行)
web_audio       浏览器 WebSocket 音频 I/O
setup_taps      entrypoint 的装配函数(事件处理器/tap 链/测试仪表)
"""
