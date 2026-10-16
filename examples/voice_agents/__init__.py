"""小歌并发部署 · 池管理器(PR-B)。

三组件:manager(进程池状态机/spawn/healthz/回收)、control_api(/alloc·/release·/status)、
transcoder(录音转码旁路任务)。均为池管理器侧独立组件,与 agent 进程生死解耦。
"""
