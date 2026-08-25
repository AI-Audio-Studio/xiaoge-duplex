#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小歌日志监控 Web 服务.

一个轻量、零依赖（仅 Python 标准库）的 web 服务，用于在浏览器里
按实例 / 日志文件分类浏览当天日志，支持关键词搜索高亮、按级别分类计数。

部署在服务器本机，直接读取本地日志文件，无鉴权（仅内网）。

用法:
    python log_monitor.py            # 前台运行
    python log_monitor.py --port 10095
    nohup python log_monitor.py > .run/log_monitor.log 2>&1 &
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# ─── 配置区 ──────────────────────────────────────────────────────────────────

INSTANCES: dict[str, dict] = {
    "10098": {
        "label": "10098 (历史备份)",
        "base": "/data/home/allen.wangmh/software/xiaoge/xiaoge-duplex-main_bak708",
        "logs": {
            "web_ui_agent": {"path": ".run/web_ui_agent.log", "format": "livekit"},
        },
    },
    "10099": {
        "label": "10099 (生产)",
        "base": "/data/home/allen.wangmh/software/xiaoge/xiaoge-duplex-main-817",
        "logs": {
            "gateway": {"path": ".run/gateway.log", "format": "stdlog"},
            "poolmgr": {"path": ".run/poolmgr.log", "format": "mixed"},
            "web_ui_agent": {"path": ".run/web_ui_agent.log", "format": "livekit"},
            "voice_qa": {"path": ".run/qwen_voice_qa_%Y%m%d.log", "format": "json"},
        },
    },
}

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 8020
# 可选 URL 前缀（nginx 反代到子路径时用，如 /xiaoge/logs/）。
# 为空则根路径；前端 fetch 会从 window.location 自动推导前缀，故此处仅影响后端路由剥离。
URL_PREFIX = "/xiaoge/logs"
MAX_TAIL_LINES = 2000  # 单次返回上限，避免大文件拖垮

# ─── 日志解析 ────────────────────────────────────────────────────────────────

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "TRACEBACK")
LEVEL_ORDER = {lv: i for i, lv in enumerate(LEVELS)}

# 标准 Python logging: 2026-08-24 11:14:16,897 INFO aiohttp.access: 消息
_RE_STDLOG = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+)\s+"
    r"(DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+(\S+):\s?(.*)$"
)
# livekit rich: HH:MM:SS.ms  LEVEL   name   消息 (开头可能有缩进)
_RE_LIVEKIT = re.compile(
    r"^\s*(\d{2}:\d{2}:\d{2}\.\d+)\s+"
    r"(DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+(\S+)\s+(.*)$"
)
# 日期提取（用于"当天"过滤）
_RE_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _today_compact() -> str:
    return datetime.now().strftime("%Y%m%d")


def resolve_log_path(base: str, log_cfg: dict) -> str | None:
    """解析日志文件绝对路径，支持 %Y%m%d 占位。不存在返回 None。"""
    rel = log_cfg["path"]
    if "%Y%m%d" in rel:
        rel = datetime.now().strftime(rel)
    full = os.path.join(base, rel)
    return full if os.path.isfile(full) else None


def _file_mtime_date(path: str) -> str | None:
    """文件 mtime 的 YYYY-MM-DD。"""
    try:
        ts = os.path.getmtime(path)
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    except OSError:
        return None


def _read_lines(path: str, limit: int = MAX_TAIL_LINES * 4) -> list[str]:
    """从尾部倒读行（带上限），去尾部截断。返回正序 list[str]。

    用 deque 风格的 collections 滚动窗口，避免大文件全量读入内存。
    """
    from collections import deque

    buf: deque[str] = deque(maxlen=limit)
    # errors='replace' 容忍 GBK/UTF-8 混入的坏字节
    try:
        with open(path, "r", encoding="utf-8", errors="replace", buffering=1) as f:
            for line in f:
                buf.append(line.rstrip("\n"))
    except OSError:
        return []
    return list(buf)


# ─── 各格式解析为统一记录 ────────────────────────────────────────────────────


def parse_stdlog(lines: list[str], today: str) -> list[dict]:
    """标准 logging 格式：按行内日期过滤当天，提取级别。"""
    out: list[dict] = []
    pending_traceback = False
    tb_lines: list[str] = []
    tb_start_lineno = 0

    def flush_tb():
        nonlocal pending_traceback, tb_lines, tb_start_lineno
        if pending_traceback and tb_lines:
            out.append({
                "lineno": tb_start_lineno,
                "level": "TRACEBACK",
                "time": "",
                "module": "",
                "text": "\n".join(tb_lines),
            })
        pending_traceback = False
        tb_lines = []

    for i, raw in enumerate(lines, 1):
        m = _RE_STDLOG.match(raw)
        if m:
            flush_tb()
            ts, level, mod, msg = m.groups()
            if ts.startswith(today):
                out.append({
                    "lineno": i,
                    "level": level,
                    "time": ts.split(" ", 1)[-1] if " " in ts else ts,
                    "module": mod,
                    "text": msg,
                })
            continue
        # Traceback 块检测
        if raw.lstrip().startswith("Traceback (most recent call last"):
            flush_tb()
            pending_traceback = True
            tb_start_lineno = i
            tb_lines = [raw.lstrip()]
        elif pending_traceback:
            # 续行：缩进行 / File / except 等都并入
            tb_lines.append(raw)
            # 一个 traceback 通常以空行或下一个正常日志行结束
            if raw.strip() == "" and len(tb_lines) > 1:
                flush_tb()
        else:
            # 非标准行且不在 traceback 块中，跳过（可能是其他格式混入）
            continue
    flush_tb()
    return out


def parse_livekit(lines: list[str], today: str, mtime_date: str | None = None) -> list[dict]:
    """livekit rich 格式：无年份，用文件 mtime 日期判定当天。

    策略：取文件 mtime 日期 == today 则整个文件的 livekit 行都算当天。
    返回最近 N 行（不按行内日期过滤，因为格式里没有日期）。
    """
    # livekit 行无日期，只能靠 mtime 判断"这个文件今天还在写"
    # 只要文件 mtime 是今天，就展示其内容；否则视为历史日志也展示（用户可看历史）
    out: list[dict] = []
    pending_traceback = False
    tb_lines: list[str] = []
    tb_start_lineno = 0
    cur: dict | None = None  # 当前正在累积多行的记录

    def flush_tb():
        nonlocal pending_traceback, tb_lines, tb_start_lineno
        if pending_traceback and tb_lines:
            out.append({
                "lineno": tb_start_lineno,
                "level": "TRACEBACK",
                "time": "",
                "module": "",
                "text": "\n".join(tb_lines),
            })
        pending_traceback = False
        tb_lines = []

    def flush_cur():
        nonlocal cur
        if cur is not None:
            out.append(cur)
        cur = None

    for i, raw in enumerate(lines, 1):
        m = _RE_LIVEKIT.match(raw)
        if m:
            flush_tb()
            flush_cur()
            ts, level, mod, msg = m.groups()
            cur = {
                "lineno": i,
                "level": level,
                "time": ts,
                "module": mod,
                "text": msg,
            }
            continue
        if raw.lstrip().startswith("Traceback (most recent call last"):
            flush_cur()
            pending_traceback = True
            tb_start_lineno = i
            tb_lines = [raw.lstrip()]
            continue
        if pending_traceback:
            tb_lines.append(raw)
            if raw.strip() == "" and len(tb_lines) > 1:
                flush_tb()
            continue
        # livekit rich 的续行：带前导空格的行并入当前记录的 text
        if cur is not None and raw.startswith(" "):
            cur["text"] += "\n" + raw.strip()
        elif cur is not None and raw.strip() == "":
            # 空行视为一条记录结束
            flush_cur()
        else:
            # 无法识别的独立行：若 cur 存在则收尾，再记录为 INFO（便于搜索）
            flush_cur()
            if raw.strip():
                cur = {
                    "lineno": i,
                    "level": "INFO",
                    "time": "",
                    "module": "",
                    "text": raw.strip(),
                }
    flush_cur()
    flush_tb()
    return out


def parse_mixed(lines: list[str], today: str) -> list[dict]:
    """混合格式（poolmgr.log）：同时含标准 logging 行和 livekit rich 行。

    策略：逐行尝试 stdlog 正则，不中则尝试 livekit 正则，都不中按续行/traceback 处理。
    当天过滤：stdlog 行按行内日期；livekit 行无日期则全收（混在同一文件里视为同时段）。
    """
    out: list[dict] = []
    pending_traceback = False
    tb_lines: list[str] = []
    tb_start_lineno = 0
    cur: dict | None = None

    def flush_tb():
        nonlocal pending_traceback, tb_lines, tb_start_lineno
        if pending_traceback and tb_lines:
            out.append({
                "lineno": tb_start_lineno,
                "level": "TRACEBACK",
                "time": "",
                "module": "",
                "text": "\n".join(tb_lines),
            })
        pending_traceback = False
        tb_lines = []

    def flush_cur():
        nonlocal cur
        if cur is not None:
            out.append(cur)
        cur = None

    for i, raw in enumerate(lines, 1):
        ms = _RE_STDLOG.match(raw)
        if ms:
            flush_tb()
            flush_cur()
            ts, level, mod, msg = ms.groups()
            if ts.startswith(today):
                out.append({
                    "lineno": i,
                    "level": level,
                    "time": ts.split(" ", 1)[-1] if " " in ts else ts,
                    "module": mod,
                    "text": msg,
                })
            continue
        ml = _RE_LIVEKIT.match(raw)
        if ml:
            flush_tb()
            flush_cur()
            ts, level, mod, msg = ml.groups()
            cur = {
                "lineno": i,
                "level": level,
                "time": ts,
                "module": mod,
                "text": msg,
            }
            continue
        if raw.lstrip().startswith("Traceback (most recent call last"):
            flush_cur()
            pending_traceback = True
            tb_start_lineno = i
            tb_lines = [raw.lstrip()]
            continue
        if pending_traceback:
            tb_lines.append(raw)
            if raw.strip() == "" and len(tb_lines) > 1:
                flush_tb()
            continue
        if cur is not None and raw.startswith(" "):
            cur["text"] += "\n" + raw.strip()
        elif cur is not None and raw.strip() == "":
            flush_cur()
        else:
            flush_cur()
            if raw.strip():
                cur = {
                    "lineno": i,
                    "level": "INFO",
                    "time": "",
                    "module": "",
                    "text": raw.strip(),
                }
    flush_cur()
    flush_tb()
    return out


def parse_json_lines(lines: list[str], today: str) -> list[dict]:
    """JSON 行格式（qwen_voice_qa_*.log）：每行一个对话记录。"""
    out: list[dict] = []
    for i, raw in enumerate(lines, 1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        ts = obj.get("timestamp", "")
        if not ts.startswith(today):
            continue
        proc = obj.get("process", "")
        asr = obj.get("asr", "")
        llm = obj.get("llm", "")
        text = f"[用户] {asr}\n[小歌] {llm}" if asr or llm else raw
        out.append({
            "lineno": i,
            "level": "INFO",
            "time": ts.split(" ", 1)[-1] if " " in ts else ts,
            "module": f"proc={proc}",
            "text": text,
        })
    return out


def parse_log(log_cfg: dict, base: str, limit: int) -> tuple[list[dict], dict]:
    """解析单个日志文件，返回 (记录列表, 元信息)。"""
    fmt = log_cfg["format"]
    path = resolve_log_path(base, log_cfg)
    meta: dict = {"format": fmt, "exists": path is not None, "path": path or log_cfg["path"]}
    if path is None:
        return [], meta
    meta["mtime"] = _file_mtime_date(path)
    today = _today_str()
    lines = _read_lines(path, limit=MAX_TAIL_LINES * 4)
    if fmt == "stdlog":
        recs = parse_stdlog(lines, today)
    elif fmt == "livekit":
        recs = parse_livekit(lines, today, meta["mtime"])
    elif fmt == "mixed":
        recs = parse_mixed(lines, today)
    elif fmt == "json":
        recs = parse_json_lines(lines, today)
    else:
        recs = []
    # 统一截断到 limit
    if len(recs) > limit:
        recs = recs[-limit:]
    meta["total"] = len(recs)
    return recs, meta


def count_levels(recs: list[dict]) -> dict[str, int]:
    cnt = {lv: 0 for lv in LEVELS}
    for r in recs:
        lv = r["level"]
        if lv in cnt:
            cnt[lv] += 1
        else:
            cnt["INFO"] += 1
    cnt["TOTAL"] = len(recs)
    return cnt


def filter_records(recs: list[dict], q: str | None, level: str | None) -> list[dict]:
    """按关键词（AND 多词）和级别过滤。"""
    out = recs
    if level and level != "ALL":
        out = [r for r in out if r["level"] == level]
    if q:
        terms = [t for t in q.split() if t]
        if terms:
            def has_all(text: str) -> bool:
                low = text.lower()
                return all(t.lower() in low for t in terms)
            out = [r for r in out if has_all(r["text"])]
    return out


# ─── HTTP 服务 ───────────────────────────────────────────────────────────────


class Handler(BaseHTTPRequestHandler):
    server_version = "xg-log-monitor/1.0"

    def log_message(self, fmt, *args):  # 静默默认访问日志
        pass

    def _send(self, body: bytes, ctype: str, code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", code)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        # 剥离可选 URL 前缀（nginx 子路径反代时可能带也可能不带前缀，两种都兼容）
        prefix = URL_PREFIX or ""
        if prefix and path.startswith(prefix):
            path = path[len(prefix):]
        if path == "" or path == "/" or path == "/index.html":
            self._send(_INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/instances":
            self._handle_instances()
            return
        if path == "/api/log":
            self._handle_log(qs)
            return
        self._send(b"not found", "text/plain; charset=utf-8", 404)

    def _handle_instances(self):
        today = _today_str()
        result = {}
        for iid, inst in INSTANCES.items():
            files = []
            for fname, fcfg in inst["logs"].items():
                path = resolve_log_path(inst["base"], fcfg)
                entry = {
                    "name": fname,
                    "format": fcfg["format"],
                    "exists": path is not None,
                    "mtime": _file_mtime_date(path) if path else None,
                    "is_today": _file_mtime_date(path) == today if path else False,
                }
                files.append(entry)
            result[iid] = {"label": inst["label"], "logs": files}
        self._json({"today": today, "instances": result})

    def _handle_log(self, qs):
        def g(k):
            v = qs.get(k, [None])
            return v[0] if v else None
        iid = g("inst")
        fname = g("file")
        q = g("q")
        level = g("level") or "ALL"
        try:
            limit = int(g("limit") or MAX_TAIL_LINES)
        except (TypeError, ValueError):
            limit = MAX_TAIL_LINES
        limit = max(1, min(limit, MAX_TAIL_LINES))
        if iid not in INSTANCES:
            self._json({"error": f"unknown inst: {iid}"}, 400)
            return
        inst = INSTANCES[iid]
        if fname not in inst["logs"]:
            self._json({"error": f"unknown file: {fname}"}, 400)
            return
        recs, meta = parse_log(inst["logs"][fname], inst["base"], limit)
        counts = count_levels(recs)
        filtered = filter_records(recs, q, level if level != "ALL" else None)
        self._json({
            "inst": iid,
            "file": fname,
            "meta": meta,
            "counts": counts,
            "level": level,
            "q": q or "",
            "total": len(filtered),
            "records": filtered,
        })


# ─── 前端页面 ────────────────────────────────────────────────────────────────

_INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>小歌日志监控</title>
<style>
  :root{
    --bg:#1a1b26; --panel:#1f2335; --border:#2a2e44; --text:#c0caf5;
    --dim:#565f89; --accent:#7aa2f7;
    --c-debug:#565f89; --c-info:#9ece6a; --c-warn:#e0af68; --c-error:#f7768e;
    --c-trace:#ff007c; --c-crit:#ff007c;
  }
  *{box-sizing:border-box}
  body{margin:0;font-family:'Cascadia Code','JetBrains Mono','Consolas',monospace;
       background:var(--bg);color:var(--text);font-size:13px;line-height:1.5}
  header{background:var(--panel);border-bottom:1px solid var(--border);padding:8px 16px;
         display:flex;align-items:center;gap:12px;flex-wrap:wrap}
  h1{font-size:15px;margin:0;font-weight:600;color:var(--accent)}
  .tabs{display:flex;gap:4px;flex-wrap:wrap}
  .tab{padding:4px 12px;border-radius:4px;cursor:pointer;background:transparent;
       border:1px solid var(--border);color:var(--dim);font-size:12px;transition:.15s}
  .tab:hover{color:var(--text);border-color:var(--accent)}
  .tab.active{background:var(--accent);color:#1a1b26;border-color:var(--accent);font-weight:600}
  .layout{display:flex;height:calc(100vh - 49px)}
  .sidebar{width:240px;background:var(--panel);border-right:1px solid var(--border);
           padding:12px;overflow-y:auto;flex-shrink:0}
  .main{flex:1;overflow-y:auto;padding:0}
  .side-block{margin-bottom:18px}
  .side-block h3{font-size:11px;color:var(--dim);margin:0 0 8px;text-transform:uppercase;letter-spacing:.5px}
  .lvl{display:flex;justify-content:space-between;align-items:center;padding:4px 8px;
       border-radius:3px;cursor:pointer;font-size:12px;margin-bottom:2px;border:1px solid transparent}
  .lvl:hover{background:#2a2e44}
  .lvl.active{border-color:var(--accent);background:#2a2e44}
  .lvl .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:8px}
  .lvl .n{font-weight:600}
  .d-debug{background:var(--c-debug)} .d-info{background:var(--c-info)}
  .d-warn{background:var(--c-warn)} .d-error{background:var(--c-error)}
  .d-trace{background:var(--c-trace)} .d-crit{background:var(--c-crit)}
  .d-total{background:var(--accent)}
  input[type=text],select{width:100%;background:#16161e;border:1px solid var(--border);
        color:var(--text);padding:6px 8px;border-radius:4px;font-family:inherit;font-size:12px}
  input[type=text]:focus{outline:none;border-color:var(--accent)}
  label{font-size:11px;color:var(--dim);display:block;margin-bottom:4px}
  .log-line{display:flex;padding:2px 12px;border-bottom:1px solid #16161e;
            font-size:12px;white-space:pre-wrap;word-break:break-word}
  .log-line:hover{background:#16161e36}
  .ln{color:var(--dim);width:52px;flex-shrink:0;user-select:none;text-align:right;padding-right:8px}
  .lv{width:72px;flex-shrink:0;font-weight:600;padding-right:8px}
  .tm{color:var(--dim);width:96px;flex-shrink:0;padding-right:8px}
  .mod{color:#7dcfff;width:120px;flex-shrink:0;padding-right:8px;overflow:hidden;
       text-overflow:ellipsis;white-space:nowrap}
  .tx{flex:1;min-width:0}
  .lv-DEBUG{color:var(--c-debug)} .lv-INFO{color:var(--c-info)}
  .lv-WARNING{color:var(--c-warn)} .lv-ERROR{color:var(--c-error)}
  .lv-TRACEBACK{color:var(--c-trace)} .lv-CRITICAL{color:var(--c-crit)}
  mark{background:#e0af68;color:#1a1b26;border-radius:2px;padding:0 1px}
  .empty{padding:40px;text-align:center;color:var(--dim)}
  .meta{font-size:11px;color:var(--dim);margin-top:4px}
  .pill{font-size:10px;padding:1px 6px;border-radius:8px;background:#2a2e44;color:var(--dim);margin-left:6px}
  .pill.ok{background:#1b2a1b;color:var(--c-info)}
  .pill.no{background:#2a1b1b;color:var(--c-error)}
</style>
</head>
<body>
<header>
  <h1>📋 小歌日志监控</h1>
  <div class="tabs" id="inst-tabs"></div>
  <div class="tabs" id="file-tabs"></div>
  <span class="pill" id="today-pill"></span>
</header>
<div class="layout">
  <aside class="sidebar">
    <div class="side-block">
      <h3>级别筛选</h3>
      <div id="level-filter"></div>
    </div>
    <div class="side-block">
      <h3>搜索（多词 AND）</h3>
      <input type="text" id="search" placeholder="关键词 空格分隔…" autocomplete="off">
    </div>
    <div class="side-block">
      <h3>自动刷新</h3>
      <select id="refresh">
        <option value="0">关</option>
        <option value="5">每 5 秒</option>
        <option value="10" selected>每 10 秒</option>
        <option value="30">每 30 秒</option>
      </select>
    </div>
    <div class="side-block">
      <h3>行数上限</h3>
      <select id="limit">
        <option value="500">500</option>
        <option value="1000">1000</option>
        <option value="2000" selected>2000</option>
      </select>
    </div>
    <div class="side-block">
      <h3>文件信息</h3>
      <div class="meta" id="meta"></div>
    </div>
  </aside>
  <main class="main" id="log-area">
    <div class="empty">选择实例和日志文件开始浏览…</div>
  </main>
</div>
<script>
const state={inst:'10099',file:'gateway',level:'ALL',q:'',limit:2000};
let timer=null;
const $=s=>document.querySelector(s);
const esc=s=>s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
// 从当前页面 URL 推导 API base 前缀，兼容根路径部署和 nginx 子路径反代。
// 如访问 /xiaoge/logs/ 则 base=/xiaoge/logs；访问 :8020/ 则 base=''。
const BASE=(function(){
  const m=location.pathname.replace(/\/+$/,'').match(/^(.*\/)?(?:index\.html)?$/);
  // 去掉可能尾部的 index.html，保留目录部分；根路径时返回空串
  let p=location.pathname.replace(/index\.html$/,'').replace(/\/+$/,'');
  return p; // 如 /xiaoge/logs 或 '' (根)
})();
function highlight(text,q){
  const safe=esc(text);
  if(!q)return safe;
  const terms=q.split(/\s+/).filter(Boolean).map(t=>t.replace(/[.*+?^${}()|[\]\\]/g,'\\$&'));
  if(!terms.length)return safe;
  const re=new RegExp('('+terms.join('|')+')','gi');
  return safe.replace(re,'<mark>$1</mark>');
}
async function api(path){
  const r=await fetch(BASE+path);
  return r.json();
}
async function loadInstances(){
  const d=await api('/api/instances');
  $('#today-pill').textContent='今天 '+d.today;
  const it=$('#inst-tabs'); it.innerHTML='';
  for(const[id,inst] of Object.entries(d.instances)){
    const t=document.createElement('div');
    t.className='tab'+(id===state.inst?' active':'');
    t.textContent=inst.label;
    t.onclick=()=>{state.inst=id;state.file=Object.keys(inst.logs)[0];
      loadInstances();loadLog();};
    it.appendChild(t);
  }
  // 文件 tab
  const cur=d.instances[state.inst];
  const ft=$('#file-tabs'); ft.innerHTML='';
  for(const f of cur.logs){
    const t=document.createElement('div');
    t.className='tab'+(f.name===state.file?' active':'');
    let lbl=f.name;
    if(f.exists){
      if(f.is_today){lbl+=' ✅';}
      else{lbl+=' ⏸';}
    }else{lbl+=' ❌';}
    t.title=f.format+' 格式';
    t.textContent=lbl;
    t.onclick=()=>{state.file=f.name;loadInstances();loadLog();};
    ft.appendChild(t);
  }
}
async function loadLog(){
  const params=new URLSearchParams({inst:state.inst,file:state.file,level:state.level,limit:state.limit});
  if(state.q)params.set('q',state.q);
  const d=await api('/api/log?'+params);
  if(d.error){$('#log-area').innerHTML='<div class="empty">'+esc(d.error)+'</div>';return;}
  const counts=d.counts;
  // 级别筛选
  const lf=$('#level-filter'); lf.innerHTML='';
  const items=[['ALL','全部',counts.TOTAL||0,'total'],
    ...[['DEBUG','调试'],['INFO','信息'],['WARNING','警告'],['ERROR','错误'],['TRACEBACK','堆栈'],['CRITICAL','致命']]
      .map(([k,l])=>[k,l,counts[k]||0,k.toLowerCase()])];
  for(const[k,label,n,cls]of items){
    const e=document.createElement('div');
    e.className='lvl'+(k===state.level?' active':'');
    e.innerHTML='<span><span class="dot d-'+cls+'"></span>'+label+'</span><span class="n">'+n+'</span>';
    e.onclick=()=>{state.level=k;loadLog();};
    lf.appendChild(e);
  }
  // 元信息
  const m=d.meta;
  $('#meta').innerHTML=esc(m.path||'')+'<br>'+
    (m.exists?('mtime: '+esc(m.mtime||'')+' · 格式: '+m.format):'<span style="color:var(--c-error)">文件不存在</span>');
  // 日志行
  const area=$('#log-area');
  if(!d.records.length){
    area.innerHTML='<div class="empty">无匹配记录</div>';return;
  }
  let h='';
  for(const r of d.records){
    const lvcls='lv-'+r.level;
    h+='<div class="log-line">'+
       '<span class="ln">'+r.lineno+'</span>'+
       '<span class="lv '+lvcls+'">'+r.level+'</span>'+
       '<span class="tm">'+esc(r.time||'')+'</span>'+
       '<span class="mod" title="'+esc(r.module||'')+'">'+esc(r.module||'')+'</span>'+
       '<span class="tx">'+highlight(r.text||'',state.q)+'</span>'+
       '</div>';
  }
  area.innerHTML=h;
  area.scrollTop=area.scrollHeight;
}
function scheduleRefresh(){
  if(timer)clearInterval(timer);
  const v=parseInt($('#refresh').value,10);
  if(v>0)timer=setInterval(loadLog,v*1000);
}
$('#search').addEventListener('input',e=>{
  clearTimeout(state._t);
  state._t=setTimeout(()=>{state.q=e.target.value.trim();loadLog();},300);
});
$('#refresh').addEventListener('change',scheduleRefresh);
$('#limit').addEventListener('change',e=>{state.limit=parseInt(e.target.value,10);loadLog();});
// init
(async()=>{
  try{await loadInstances();await loadLog();}catch(e){
    $('#log-area').innerHTML='<div class="empty">加载失败: '+esc(String(e))+'</div>';
  }
  scheduleRefresh();
})();
</script>
</body>
</html>
"""


# ─── 启动 ────────────────────────────────────────────────────────────────────


def main():
    ap = argparse.ArgumentParser(description="小歌日志监控 web 服务")
    ap.add_argument("--host", default=LISTEN_HOST)
    ap.add_argument("--port", type=int, default=LISTEN_PORT)
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[log-monitor] listening on http://{args.host}:{args.port}", flush=True)
    print(f"[log-monitor] instances: {list(INSTANCES.keys())}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[log-monitor] shutting down", flush=True)
        srv.shutdown()


if __name__ == "__main__":
    main()
