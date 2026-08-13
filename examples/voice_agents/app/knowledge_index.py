"""知识库内嵌向量检索(方案 B)。

设计:
- 文档:Markdown,按 `# ` header 切块(每块带标题,便于 LLM 引用)
- Embedding:DashScope OpenAI 兼容 endpoint,text-embedding-v3,1024 维
- 存储:SQLite(knowledge.db)+ numpy 向量矩阵(.npy)
- 检索:余弦相似度,top-k
- 热更新:replace_index_dir() 替换索引文件后下次 query 自动 reload

环境变量:
- DASHSCOPE_API_KEY:必填,embedding 调用凭证
- DASHSCOPE_BASE_URL:默认 https://dashscope.aliyuncs.com/compatible-mode/v1
- XIAOGE_KNOWLEDGE_EMBED_MODEL:默认 text-embedding-v3
- XIAOGE_KNOWLEDGE_DIR:语料 Markdown 目录(默认 data/knowledge)
- XIAOGE_KNOWLEDGE_INDEX_DIR:索引持久化目录(默认 data/knowledge_index)
- XIAOGE_KNOWLEDGE_DIM:向量维度,默认 1024(text-embedding-v3)
- XIAOGE_KNOWLEDGE_TOP_K:默认 5
- XIAOGE_KNOWLEDGE_MIN_SCORE:默认 0.30,低于此分数不返回
- XIAOGE_KNOWLEDGE_OFFLINE:离线模式(不调 embedding API,用 hash 伪向量)
  仅供测试,生产不可用
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

logger = logging.getLogger("web-ui-agent")

DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_EMBED_MODEL = "text-embedding-v3"
DEFAULT_DIM = 1024
DEFAULT_TOP_K = 5
DEFAULT_MIN_SCORE = 0.30
MAX_CHUNK_CHARS = 800  # 超长块再切,避免单块过大
MIN_CHUNK_CHARS = 30  # 过短块并入下一块

HEADER_RE = re.compile(r"^# ", re.MULTILINE)


@dataclass
class KnowledgeHit:
    """单条检索命中。"""

    text: str
    score: float
    source: str
    title: str


@dataclass
class _IndexMeta:
    """索引元数据,持久化到 SQLite。"""

    source_dir: str
    embed_model: str
    dim: int
    doc_count: int
    built_at: float


class KnowledgeIndex:
    """知识库索引:切块 + embedding + 检索。

    线程语义:rebuild/query 都在 agent 循环线程或 CLI 调用,内部状态无锁。
    _load() 检查 mtime,文件变化时自动 reload(支持热更新)。
    """

    def __init__(
        self,
        *,
        source_dir: Path | str | None = None,
        index_dir: Path | str | None = None,
        embed_model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        dim: int | None = None,
        offline: bool | None = None,
        top_k: int | None = None,
        min_score: float | None = None,
    ) -> None:
        self.source_dir = Path(
            source_dir or os.getenv("XIAOGE_KNOWLEDGE_DIR", "data/knowledge")
        ).resolve()
        self.index_dir = Path(
            index_dir or os.getenv("XIAOGE_KNOWLEDGE_INDEX_DIR", "data/knowledge_index")
        ).resolve()
        self.embed_model = embed_model or os.getenv(
            "XIAOGE_KNOWLEDGE_EMBED_MODEL", DEFAULT_EMBED_MODEL
        )
        self.base_url = base_url or os.getenv("DASHSCOPE_BASE_URL", DEFAULT_BASE_URL)
        self.api_key = api_key or os.getenv("DASHSCOPE_API_KEY", "")
        self.dim = int(
            dim if dim is not None else os.getenv("XIAOGE_KNOWLEDGE_DIM", str(DEFAULT_DIM))
        )
        self.offline = bool(
            offline
            if offline is not None
            else os.getenv("XIAOGE_KNOWLEDGE_OFFLINE", "0") in ("1", "true", "yes")
        )
        self.top_k = int(
            top_k if top_k is not None else os.getenv("XIAOGE_KNOWLEDGE_TOP_K", str(DEFAULT_TOP_K))
        )
        self.min_score = float(
            min_score
            if min_score is not None
            else os.getenv("XIAOGE_KNOWLEDGE_MIN_SCORE", str(DEFAULT_MIN_SCORE))
        )

        # 索引状态
        self._meta: _IndexMeta | None = None
        self._texts: list[str] = []
        self._sources: list[str] = []
        self._titles: list[str] = []
        self._vectors: np.ndarray | None = None  # shape (N, dim)
        self._loaded_at = 0.0
        self._index_mtime = 0.0
        self._lock = asyncio.Lock()

    # ─────────────────────────── 切块 ───────────────────────────

    def _split_markdown(self, content: str, source: str) -> list[tuple[str, str]]:
        """按 `# ` header 切块,每块带标题。返回 [(title, body), ...]。

        切块策略:
        1. 按 `^# ` 切分,每段含标题行;
        2. 标题去掉 `# ` 前缀,作为 title;
        3. body 超 MAX_CHUNK_CHARS 再按句号切分。
        """
        # 把内容按 `^# ` 切分;首段若无标题则丢弃(通常是文件头部空行)
        parts = HEADER_RE.split(content)
        chunks: list[tuple[str, str]] = []
        for part in parts:
            part = part.strip()
            if not part:
                continue
            # 第一行是标题(被 # split 后无 # 前缀)
            lines = part.split("\n", 1)
            title = lines[0].strip()
            body = lines[1].strip() if len(lines) > 1 else ""
            if not body:
                continue
            # 超长块再切
            if len(body) > MAX_CHUNK_CHARS:
                for sub in self._split_long_body(body):
                    chunks.append((title, sub))
            else:
                chunks.append((title, body))
        return [(t, b) for t, b in chunks if b]

    def _split_long_body(self, body: str) -> list[str]:
        """超长 body 按句号切,每块 ≤ MAX_CHUNK_CHARS。"""
        out: list[str] = []
        # 优先按句号、问号、感叹号切
        sentences = re.split(r"(?<=[。!?\n])", body)
        buf = ""
        for s in sentences:
            if not s:
                continue
            if len(buf) + len(s) <= MAX_CHUNK_CHARS:
                buf += s
            else:
                if buf:
                    out.append(buf)
                buf = s
        if buf:
            out.append(buf)
        return out

    def _scan_source_files(self) -> list[Path]:
        """扫描 source_dir 下所有 .md 文件,按路径排序保证索引稳定。"""
        if not self.source_dir.exists():
            return []
        files = sorted(self.source_dir.rglob("*.md"))
        return files

    def append_user_chunk(self, body: str, *, title: str | None = None) -> Path:
        """把一条用户知识追加到 source_dir/user_knowledge.md。

        每个 entry = `# <title>\\n\\n<body>\\n`;rebuild 时切块器自动切到这块。
        body 内若含 `^# ` 行,会被切块器切成多个独立块(允许用户用 # 拆分多个知识点)。

        Returns: 写入的文件路径。
        """
        from datetime import datetime

        self.source_dir.mkdir(parents=True, exist_ok=True)
        path = self.source_dir / "user_knowledge.md"
        safe_title = (title or f"用户补充 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}").replace(
            "#", ""
        ).strip() or "用户补充"
        body = body.strip()
        if not body:
            raise ValueError("append_user_chunk: body is empty")
        entry = f"\n\n# {safe_title}\n\n{body}\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(entry)
        logger.info(
            "appended user chunk to %s (title=%r, body_len=%d)", path, safe_title, len(body)
        )
        return path

    # ─────────────────────────── 列表 / 删除 ───────────────────────────

    def list_chunks(self) -> list[dict]:
        """列出所有块,返回 [{id, title, source, body, body_preview, deletable}, ...]。

        - body 是嵌入时用的完整文本(`{title}\\n{body}`),用于前端展示与定位删除;
        - body_preview 截前 100 字,便于卡片显示;
        - deletable: source == "user_knowledge.md" 才允许删,产品手册只读。
        无索引(未 rebuild/未 _load)时返回空列表。
        """
        db_path = self.index_dir / "knowledge.db"
        if not db_path.exists():
            return []
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute("SELECT id, title, source, body FROM chunks ORDER BY id").fetchall()
        finally:
            conn.close()
        out: list[dict] = []
        for rid, title, source, body in rows:
            body_str = body or ""
            # body 实际是 `f"{title}\n{body}"`,去掉首行标题得到纯正文
            pure_body = body_str.split("\n", 1)[1] if "\n" in body_str else body_str
            preview = pure_body.replace("\n", " ").strip()[:100]
            out.append(
                {
                    "id": rid,
                    "title": title or "",
                    "source": source or "",
                    "body": pure_body,
                    "body_preview": preview,
                    "deletable": (source or "") == "user_knowledge.md",
                }
            )
        return out

    async def delete_chunk(self, chunk_id: int) -> bool:
        """删除指定 chunk。仅 user_knowledge.md 来源的块可删。

        流程:
        1. 从 SQLite 取 title/source/body;
        2. 校验 source == "user_knowledge.md",否则拒绝;
        3. 从 user_knowledge.md 删除对应 `# <title>\\n\\n<body>\\n` 段(用 title+body 双匹配,避免同名误删);
        4. rebuild() 重写索引。

        Returns: 是否删除成功。
        """
        db_path = self.index_dir / "knowledge.db"
        if not db_path.exists():
            logger.warning("delete_chunk: no index db at %s", db_path)
            return False
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT title, source, body FROM chunks WHERE id = ?", (int(chunk_id),)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            logger.warning("delete_chunk: chunk id=%s not found", chunk_id)
            return False
        title, source, body = row
        if source != "user_knowledge.md":
            logger.warning("delete_chunk: chunk id=%s source=%r not deletable", chunk_id, source)
            return False
        path = self.source_dir / "user_knowledge.md"
        if not path.exists():
            logger.warning("delete_chunk: %s missing", path)
            return False
        text = path.read_text(encoding="utf-8")
        # entry 格式: `\n\n# {title}\n\n{body}\n`,其中 body 是嵌入文本(`{title}\n{原始正文}`)
        # 切块器写入 db 的 body = f"{title}\n{原始正文}",所以原始正文 = body 去掉首行
        pure_body = (body or "").split("\n", 1)[1] if "\n" in (body or "") else (body or "")
        entry = f"# {title}\n\n{pure_body}\n"
        if entry not in text:
            # 兜底:仅按 title 匹配(可能 body 因 split_long_body 被改写)
            head = f"# {title}\n\n"
            if head not in text:
                logger.warning("delete_chunk: entry not found in %s (title=%r)", path, title)
                return False
            start = text.index(head)
            next_idx = text.find("\n\n# ", start + len(head))
            end = len(text) if next_idx == -1 else next_idx
            new_text = text[:start] + text[end:]
        else:
            new_text = text.replace(entry, "", 1)
        path.write_text(new_text, encoding="utf-8")
        logger.info("deleted chunk id=%s title=%r from %s, rebuilding", chunk_id, title, path)
        await self.rebuild()
        return True

    # ─────────────────────────── embedding ───────────────────────────

    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        """调 DashScope embedding API(OpenAI 兼容协议)。"""
        if self.offline:
            return [self._hash_pseudo_vector(t) for t in texts]
        if not self.api_key:
            raise RuntimeError(
                "DASHSCOPE_API_KEY not set; cannot embed. "
                "Set XIAOGE_KNOWLEDGE_OFFLINE=1 for testing."
            )
        # 局部 import,避免无 openai 时 import 整个模块失败
        import openai

        client = openai.AsyncClient(
            api_key=self.api_key,
            base_url=self.base_url,
            max_retries=2,
            timeout=30.0,
        )
        # DashScope 单批最多 10 条(2026-08 起 API 强制 ≤10);切分
        out: list[list[float]] = []
        batch_size = 10
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            try:
                resp = await client.embeddings.create(
                    model=self.embed_model,
                    input=batch,
                )
                for item in resp.data:
                    out.append(item.embedding)
            except Exception:
                logger.exception("embedding batch failed (offset=%d, batch=%d)", i, len(batch))
                # 失败的 batch 用零向量占位,保证索引不丢块
                for _ in batch:
                    out.append([0.0] * self.dim)
        return out

    def _hash_pseudo_vector(self, text: str) -> list[float]:
        """离线模式:用 hash 生成伪向量(仅供测试)。

        把 text 滑窗分词,每个词 hash 到 [0, dim) 的位置上 +1。
        这样语义相似的文本(hash 碰撞多)向量相近,可粗略测检索。
        """
        v = [0.0] * self.dim
        for i in range(len(text) - 1):
            bigram = text[i : i + 2]
            h = int(hashlib.md5(bigram.encode("utf-8")).hexdigest(), 16)
            v[h % self.dim] += 1.0
        # L2 归一化
        norm = sum(x * x for x in v) ** 0.5
        if norm > 0:
            v = [x / norm for x in v]
        return v

    # ─────────────────────────── 重建索引 ───────────────────────────

    async def rebuild(self) -> int:
        """重建索引:扫源文件 → 切块 → embed → 持久化。返回块数。"""
        files = self._scan_source_files()
        if not files:
            logger.warning("knowledge rebuild: no .md files in %s", self.source_dir)
            return 0
        all_texts: list[str] = []
        all_titles: list[str] = []
        all_sources: list[str] = []
        for f in files:
            try:
                content = f.read_text(encoding="utf-8")
            except Exception:
                logger.exception("failed to read %s", f)
                continue
            chunks = self._split_markdown(content, str(f.relative_to(self.source_dir)))
            for title, body in chunks:
                # 把 title 拼进 text,让 embedding 捕捉标题语义
                all_texts.append(f"{title}\n{body}")
                all_titles.append(title)
                all_sources.append(str(f.relative_to(self.source_dir)))
        if not all_texts:
            logger.warning("knowledge rebuild: no chunks after split")
            return 0
        logger.info(
            "knowledge rebuild: %d files → %d chunks, embedding...",
            len(files),
            len(all_texts),
        )
        vectors = await self._embed_batch(all_texts)
        # 持久化
        self.index_dir.mkdir(parents=True, exist_ok=True)
        npy_path = self.index_dir / "vectors.npy"
        db_path = self.index_dir / "knowledge.db"
        meta_path = self.index_dir / "meta.json"
        # 向量矩阵
        arr = np.array(vectors, dtype=np.float32)
        np.save(npy_path, arr)
        # SQLite 存文本与元数据
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("DROP TABLE IF EXISTS chunks")
            conn.execute(
                """
                CREATE TABLE chunks (
                    id INTEGER PRIMARY KEY,
                    title TEXT,
                    source TEXT,
                    body TEXT
                )
                """
            )
            for i, (title, source, body) in enumerate(
                zip(all_titles, all_sources, all_texts, strict=True)
            ):
                conn.execute(
                    "INSERT INTO chunks (id, title, source, body) VALUES (?, ?, ?, ?)",
                    (i, title, source, body),
                )
            conn.commit()
        finally:
            conn.close()
        # meta
        meta = _IndexMeta(
            source_dir=str(self.source_dir),
            embed_model=self.embed_model,
            dim=self.dim,
            doc_count=len(all_texts),
            built_at=time.time(),
        )
        meta_path.write_text(
            json.dumps(meta.__dict__, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # 加载到内存
        self._meta = meta
        self._texts = all_texts
        self._titles = all_titles
        self._sources = all_sources
        self._vectors = arr
        self._loaded_at = time.time()
        self._index_mtime = meta_path.stat().st_mtime
        logger.info(
            "knowledge rebuild done: %d chunks, dim=%d, persisted to %s",
            len(all_texts),
            self.dim,
            self.index_dir,
        )
        return len(all_texts)

    # ─────────────────────────── 加载/热更新 ───────────────────────────

    def _load(self) -> bool:
        """从磁盘加载索引。返回是否成功加载。"""
        npy_path = self.index_dir / "vectors.npy"
        db_path = self.index_dir / "knowledge.db"
        meta_path = self.index_dir / "meta.json"
        if not (npy_path.exists() and db_path.exists() and meta_path.exists()):
            return False
        try:
            meta_dict = json.loads(meta_path.read_text(encoding="utf-8"))
            meta = _IndexMeta(**meta_dict)
            arr = np.load(npy_path)
            conn = sqlite3.connect(str(db_path))
            try:
                rows = conn.execute(
                    "SELECT id, title, source, body FROM chunks ORDER BY id"
                ).fetchall()
            finally:
                conn.close()
            self._meta = meta
            self._vectors = arr
            self._texts = [r[3] for r in rows]
            self._titles = [r[1] for r in rows]
            self._sources = [r[2] for r in rows]
            self._loaded_at = time.time()
            self._index_mtime = meta_path.stat().st_mtime
            logger.info(
                "knowledge index loaded: %d chunks, dim=%d, built_at=%s",
                meta.doc_count,
                meta.dim,
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(meta.built_at)),
            )
            return True
        except Exception:
            logger.exception("failed to load knowledge index from %s", self.index_dir)
            return False

    def _maybe_reload(self) -> None:
        """检查索引文件 mtime,变化则 reload。"""
        meta_path = self.index_dir / "meta.json"
        if not meta_path.exists():
            return
        try:
            mtime = meta_path.stat().st_mtime
        except OSError:
            return
        if mtime != self._index_mtime:
            logger.info(
                "knowledge index file changed (mtime %s → %s), reloading",
                self._index_mtime,
                mtime,
            )
            self._load()

    # ─────────────────────────── 检索 ───────────────────────────

    async def query(self, q: str, top_k: int | None = None) -> list[KnowledgeHit]:
        """检索 top-k 相关块。返回按分数降序。

        若索引未加载,尝试 _load();加载失败(无索引文件)返回空列表。
        """
        async with self._lock:
            if self._vectors is None:
                if not self._load():
                    logger.warning("knowledge query: no index loaded, returning empty")
                    return []
            self._maybe_reload()
            if self._vectors is None or self._vectors.size == 0:
                return []
            # embed query
            try:
                q_vecs = await self._embed_batch([q])
                q_vec = np.array(q_vecs[0], dtype=np.float32)
            except Exception:
                logger.exception("knowledge query: embed failed for query=%r", q)
                return []
            # 余弦相似度
            # vectors: (N, dim), q_vec: (dim,)
            vecs = self._vectors
            # 跳过零向量块(embedding 失败的占位)
            norms = np.linalg.norm(vecs, axis=1, keepdims=True)
            q_norm = np.linalg.norm(q_vec)
            if q_norm < 1e-9:
                return []
            # 避免除零
            safe_norms = np.where(norms < 1e-9, 1.0, norms)
            scores = (vecs @ q_vec) / (safe_norms.flatten() * q_norm)
            # 取 top_k
            k = min(top_k or self.top_k, len(scores))
            # argsort 降序
            top_idx = np.argsort(-scores)[:k]
            hits: list[KnowledgeHit] = []
            for idx in top_idx:
                score = float(scores[idx])
                if score < self.min_score:
                    continue
                hits.append(
                    KnowledgeHit(
                        text=self._texts[idx],
                        score=score,
                        source=self._sources[idx],
                        title=self._titles[idx],
                    )
                )
            return hits

    def is_ready(self) -> bool:
        """索引是否已加载且非空。"""
        return self._vectors is not None and self._vectors.size > 0

    @property
    def doc_count(self) -> int:
        """索引块数。"""
        return int(self._vectors.shape[0]) if self._vectors is not None else 0


# ─────────────────────────── CLI 入口 ───────────────────────────


async def _cli_rebuild() -> int:
    """命令行:python -m app.knowledge_index rebuild"""
    idx = KnowledgeIndex()
    n = await idx.rebuild()
    print(f"rebuilt: {n} chunks")
    return 0 if n > 0 else 1


async def _cli_query(q: str) -> int:
    """命令行:python -m app.knowledge_index query "问题" """
    idx = KnowledgeIndex()
    hits = await idx.query(q)
    if not hits:
        print("(no hits)")
        return 1
    for h in hits:
        print(f"\n[score={h.score:.3f}] {h.source} :: {h.title}")
        print(h.text[:300])
    return 0


def main() -> int:
    import sys

    if len(sys.argv) < 2:
        print("usage: python -m app.knowledge_index [rebuild|query <q>]")
        return 2
    cmd = sys.argv[1]
    if cmd == "rebuild":
        return asyncio.run(_cli_rebuild())
    if cmd == "query":
        if len(sys.argv) < 3:
            print("usage: python -m app.knowledge_index query <question>")
            return 2
        return asyncio.run(_cli_query(sys.argv[2]))
    print(f"unknown command: {cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
