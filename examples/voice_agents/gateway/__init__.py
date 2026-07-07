"""小歌并发部署 · 前置网关(PR-C)。

HTTP 反代 + 会话亲和 + 宽限窗 + 准入/限流,单端口同服浏览器多用户与协议客户端。
模块:config(env)、affinity(cookie+会话状态机)、pool_client(池控制 API)、proxy(反代+WS 泵)、
main(六路由规则+TLS+安全)。
"""
