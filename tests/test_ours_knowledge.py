"""行为锁定测试:app/knowledge_index.KnowledgeIndex。

覆盖:
- _split_markdown: 按 # 切块 / 超长再切 / 过短并入 / 无标题丢弃
- _embed_batch: 离线模式(hash 伪向量)/ API 失败降级
- rebuild: 扫描多文件 / 持久化 .npy + .db + meta.json
- query: top-k / min_score 过滤 / 零向量块跳过 / 空库返回 []
- 热更新: meta.json mtime 变化 → reload
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "voice_agents"))

from app.knowledge_index import (  # noqa: E402
    MAX_CHUNK_CHARS,
    KnowledgeHit,
    KnowledgeIndex,
)

# ──────────────────────────── 测试夹具 ────────────────────────────


@pytest.fixture()
def source_dir(tmp_path: Path) -> Path:
    """写 2 个 .md 文件,共 4 段。"""
    d = tmp_path / "knowledge"
    d.mkdir()
    (d / "intro.md").write_text(
        """# 小歌是什么

小歌是一款全双工语音智能体,端侧+云端协同。

# 小歌硬件

4-mic 阵列,3 米拾音,内置 3W 喇叭。
""",
        encoding="utf-8",
    )
    (d / "faq.md").write_text(
        """# 网络要求

最低 2Mbps 上行,延迟 100ms 内。

# 如何升级模型

修改 QWEN_MODEL 环境变量后重启。
""",
        encoding="utf-8",
    )
    return d


@pytest.fixture()
def offline_idx(source_dir: Path, tmp_path: Path) -> KnowledgeIndex:
    """离线模式索引(source_dir + index_dir 都在 tmp_path 下)。"""
    return KnowledgeIndex(
        source_dir=source_dir,
        index_dir=tmp_path / "knowledge_index",
        offline=True,
        dim=64,  # 小维度加速测试
    )


# ──────────────────────────── _split_markdown ────────────────────────────


class TestSplitMarkdown:
    def test_split_by_header(self, offline_idx: KnowledgeIndex) -> None:
        content = "# 标题A\n\n内容A\n\n# 标题B\n\n内容B\n"
        chunks = offline_idx._split_markdown(content, "test.md")
        assert len(chunks) == 2
        assert chunks[0] == ("标题A", "内容A")
        assert chunks[1] == ("标题B", "内容B")

    def test_skip_no_header_prefix(self, offline_idx: KnowledgeIndex) -> None:
        content = "前言\n\n# 标题\n\n内容"
        chunks = offline_idx._split_markdown(content, "test.md")
        # "前言" 没标题前缀,被丢弃
        assert len(chunks) == 1
        assert chunks[0][0] == "标题"

    def test_skip_empty_body(self, offline_idx: KnowledgeIndex) -> None:
        content = "# 标题\n\n# 第二段\n\n内容"
        chunks = offline_idx._split_markdown(content, "test.md")
        # "标题" body 为空,被跳过
        assert len(chunks) == 1
        assert chunks[0][0] == "第二段"

    def test_split_long_body(self, offline_idx: KnowledgeIndex) -> None:
        body = "句号。" * 300  # 900 chars,超 MAX_CHUNK_CHARS=800
        content = f"# 长文\n\n{body}"
        chunks = offline_idx._split_markdown(content, "test.md")
        assert len(chunks) > 1
        for title, b in chunks:
            assert title == "长文"
            assert len(b) <= MAX_CHUNK_CHARS + 20  # 句号分割容差


# ──────────────────────────── _hash_pseudo_vector ────────────────────────────


def test_hash_pseudo_vector_normalized(offline_idx: KnowledgeIndex) -> None:
    v = offline_idx._hash_pseudo_vector("测试文本内容")
    assert len(v) == offline_idx.dim
    # L2 归一化后模应为 1(或 0,若全无 hash 碰撞)
    norm = sum(x * x for x in v) ** 0.5
    assert norm == pytest.approx(1.0, abs=1e-3) or norm == 0.0


def test_hash_pseudo_vector_similar_text(offline_idx: KnowledgeIndex) -> None:
    """相似文本的 hash 向量余弦相似度应高于无关文本。"""
    v1 = offline_idx._hash_pseudo_vector("小歌硬件规格")
    v2 = offline_idx._hash_pseudo_vector("小歌硬件配置")
    v3 = offline_idx._hash_pseudo_vector("网络要求")
    arr = np.array([v1, v2, v3], dtype=np.float32)
    # 余弦相似度
    norm = np.linalg.norm(arr, axis=1, keepdims=True)
    safe = np.where(norm < 1e-9, 1.0, norm)
    normed = arr / safe
    sim_12 = float(normed[0] @ normed[1])
    sim_13 = float(normed[0] @ normed[2])
    # "小歌硬件规格" vs "小歌硬件配置" 共享 bigram 更多,应更相似
    assert sim_12 >= sim_13, f"sim_12={sim_12} should >= sim_13={sim_13}"


# ──────────────────────────── _embed_batch ────────────────────────────


def test_embed_batch_offline(offline_idx: KnowledgeIndex) -> None:
    async def run() -> None:
        vecs = await offline_idx._embed_batch(["abc", "def"])
        assert len(vecs) == 2
        assert all(len(v) == offline_idx.dim for v in vecs)

    asyncio.run(run())


def test_embed_batch_api_failure_falls_back_to_zero() -> None:
    """非离线模式 + API 失败 → batch 用零向量占位(不抛)。"""
    idx = KnowledgeIndex(
        source_dir="data/knowledge",
        index_dir="data/knowledge_index_test",
        offline=False,
        api_key="sk-fake",
        dim=8,
    )
    # mock openai.AsyncClient.embeddings.create 抛异常
    import openai

    class _FakeResp:
        embeddings: list = []

    class _FakeEmbeddings:
        async def create(self, **kwargs: object) -> None:
            raise RuntimeError("api down")

    class _FakeAsyncClient:
        embeddings = _FakeEmbeddings()

    async def run() -> None:
        with patch.object(openai, "AsyncClient", return_value=_FakeAsyncClient()):
            vecs = await idx._embed_batch(["a", "b", "c"])
            assert len(vecs) == 3
            assert all(v == [0.0] * 8 for v in vecs)

    asyncio.run(run())


# ──────────────────────────── rebuild ────────────────────────────


def test_rebuild_persists_files(offline_idx: KnowledgeIndex, source_dir: Path) -> None:
    async def run() -> None:
        n = await offline_idx.rebuild()
        assert n == 4  # 2 文件 × 2 段
        # 持久化文件
        idx_dir = offline_idx.index_dir
        assert (idx_dir / "vectors.npy").exists()
        assert (idx_dir / "knowledge.db").exists()
        assert (idx_dir / "meta.json").exists()
        # 向量矩阵 shape
        arr = np.load(idx_dir / "vectors.npy")
        assert arr.shape == (4, offline_idx.dim)
        # meta.json
        meta = json.loads((idx_dir / "meta.json").read_text(encoding="utf-8"))
        assert meta["doc_count"] == 4
        assert meta["dim"] == offline_idx.dim
        assert meta["source_dir"] == str(source_dir.resolve())

    asyncio.run(run())


def test_rebuild_empty_source_dir(tmp_path: Path) -> None:
    """source_dir 不存在或空 → rebuild 返回 0。"""
    idx = KnowledgeIndex(
        source_dir=tmp_path / "nope",
        index_dir=tmp_path / "idx",
        offline=True,
        dim=8,
    )
    async def run() -> None:
        n = await idx.rebuild()
        assert n == 0

    asyncio.run(run())


def test_rebuild_skips_files_without_headers(tmp_path: Path) -> None:
    """无 # 标题的 .md 文件 → 0 块。"""
    d = tmp_path / "knowledge"
    d.mkdir()
    (d / "noheaders.md").write_text("这只是普通文本,没有 markdown header。", encoding="utf-8")
    idx = KnowledgeIndex(
        source_dir=d,
        index_dir=tmp_path / "idx",
        offline=True,
        dim=8,
    )
    async def run() -> None:
        n = await idx.rebuild()
        assert n == 0

    asyncio.run(run())


# ──────────────────────────── query ────────────────────────────


def test_query_returns_relevant(offline_idx: KnowledgeIndex) -> None:
    """query "硬件" 应命中硬件相关块。"""
    async def run() -> None:
        await offline_idx.rebuild()
        hits = await offline_idx.query("硬件", top_k=2)
        assert len(hits) > 0
        # top hit 应是"小歌硬件" 块(标题命中)
        assert "硬件" in hits[0].title or "硬件" in hits[0].text

    asyncio.run(run())


def test_query_no_index_returns_empty(tmp_path: Path) -> None:
    """未 rebuild 也无持久化索引 → query 返回空。"""
    idx = KnowledgeIndex(
        source_dir=tmp_path / "knowledge",
        index_dir=tmp_path / "idx",  # 不存在
        offline=True,
        dim=8,
    )
    async def run() -> None:
        hits = await idx.query("anything")
        assert hits == []

    asyncio.run(run())


def test_query_min_score_filter(tmp_path: Path) -> None:
    """min_score 设很高 → 几乎所有 hit 都被过滤。"""
    d = tmp_path / "knowledge"
    d.mkdir()
    (d / "a.md").write_text("# 苹果\n\n红色的水果\n", encoding="utf-8")
    (d / "b.md").write_text("# 香蕉\n\n黄色的水果\n", encoding="utf-8")
    idx = KnowledgeIndex(
        source_dir=d,
        index_dir=tmp_path / "idx",
        offline=True,
        dim=32,
        min_score=0.99,  # 极高
    )
    async def run() -> None:
        await idx.rebuild()
        hits = await idx.query("汽车", top_k=2)
        # 离线 hash 向量,汽车 vs 苹果/香蕉相似度肯定 < 0.99
        assert hits == []

    asyncio.run(run())


def test_query_top_k_limit(offline_idx: KnowledgeIndex) -> None:
    async def run() -> None:
        await offline_idx.rebuild()
        hits = await offline_idx.query("小歌", top_k=2)
        assert len(hits) <= 2

    asyncio.run(run())


# ──────────────────────────── 热更新 ────────────────────────────


def test_hot_reload_on_meta_mtime_change(offline_idx: KnowledgeIndex) -> None:
    """meta.json mtime 变化 → _maybe_reload 触发 reload。"""
    async def run() -> None:
        await offline_idx.rebuild()
        old_texts = list(offline_idx._texts)
        assert len(old_texts) == 4
        # 改 meta.json mtime(用 touch + 内容微调保证 mtime 真的变)
        meta_path = offline_idx.index_dir / "meta.json"
        # 等一下确保 mtime 不同
        time.sleep(0.05)
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["doc_count"] = 999  # 故意改一个值
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        # _maybe_reload 应触发 _load,重新从 SQLite 读 texts(仍是 4,因为没改 db)
        offline_idx._maybe_reload()
        # meta 被重载为 999
        assert offline_idx._meta is not None
        assert offline_idx._meta.doc_count == 999
        # texts 从 db 读,仍是 4
        assert len(offline_idx._texts) == 4

    asyncio.run(run())


def test_is_ready(offline_idx: KnowledgeIndex) -> None:
    """未 rebuild:is_ready=False;rebuild 后:True。"""
    assert offline_idx.is_ready() is False
    async def run() -> None:
        await offline_idx.rebuild()
        assert offline_idx.is_ready() is True
        assert offline_idx.doc_count == 4

    asyncio.run(run())


def test_load_from_persisted_index(offline_idx: KnowledgeIndex, source_dir: Path) -> None:
    """另一个 KnowledgeIndex 实例从同一 index_dir 加载,应读到相同数据。"""
    async def run() -> None:
        await offline_idx.rebuild()
        # 新实例
        idx2 = KnowledgeIndex(
            source_dir=source_dir,
            index_dir=offline_idx.index_dir,
            offline=True,
            dim=offline_idx.dim,
        )
        assert idx2.is_ready() is False
        ok = idx2._load()
        assert ok is True
        assert idx2.is_ready() is True
        assert idx2.doc_count == 4
        # 检索结果应一致
        hits1 = await offline_idx.query("硬件", top_k=2)
        hits2 = await idx2.query("硬件", top_k=2)
        assert [h.title for h in hits1] == [h.title for h in hits2]

    asyncio.run(run())


# ──────────────────────────── KnowledgeHit ────────────────────────────


def test_knowledge_hit_dataclass() -> None:
    h = KnowledgeHit(text="abc", score=0.5, source="s.md", title="t")
    assert h.text == "abc"
    assert h.score == 0.5
    assert h.source == "s.md"
    assert h.title == "t"


# ──────────────────────────── append_user_chunk ────────────────────────────


def test_append_user_chunk_creates_file(tmp_path: Path) -> None:
    """首次追加 → source_dir/user_knowledge.md 被创建,格式 `# title\n\nbody\n`。"""
    idx = KnowledgeIndex(
        source_dir=tmp_path / "knowledge",
        index_dir=tmp_path / "idx",
        offline=True,
        dim=8,
    )
    path = idx.append_user_chunk("蓝牙5.0,可外接音箱。", title="蓝牙规格")
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "# 蓝牙规格" in content
    assert "蓝牙5.0,可外接音箱。" in content
    # 两个 # 行之间应有两个换行(Markdown 标准)
    assert "\n\n# 蓝牙规格\n\n" in content or content.startswith("# 蓝牙规格\n\n")


def test_append_user_chunk_appends_existing(tmp_path: Path) -> None:
    """已有 user_knowledge.md → 追加到末尾,不覆盖。"""
    idx = KnowledgeIndex(
        source_dir=tmp_path / "knowledge",
        index_dir=tmp_path / "idx",
        offline=True,
        dim=8,
    )
    idx.append_user_chunk("第一条。", title="标题A")
    idx.append_user_chunk("第二条。", title="标题B")
    content = (tmp_path / "knowledge" / "user_knowledge.md").read_text(encoding="utf-8")
    assert content.count("# 标题A") == 1
    assert content.count("# 标题B") == 1
    # 顺序:A 在 B 前
    assert content.index("# 标题A") < content.index("# 标题B")


def test_append_user_chunk_empty_body_raises(tmp_path: Path) -> None:
    idx = KnowledgeIndex(
        source_dir=tmp_path / "knowledge",
        index_dir=tmp_path / "idx",
        offline=True,
        dim=8,
    )
    with pytest.raises(ValueError):
        idx.append_user_chunk("   \n  ", title="x")


def test_append_user_chunk_strips_title_hashes(tmp_path: Path) -> None:
    """标题里的 # 字符应被剔除,避免切块器误切。"""
    idx = KnowledgeIndex(
        source_dir=tmp_path / "knowledge",
        index_dir=tmp_path / "idx",
        offline=True,
        dim=8,
    )
    idx.append_user_chunk("body", title="## 复杂 # 标题 ##")
    content = (tmp_path / "knowledge" / "user_knowledge.md").read_text(encoding="utf-8")
    # 标题被清理成 "复杂  标题"
    assert "# 复杂  标题" in content
    # 不应出现 ### 三连号
    assert "###" not in content


def test_append_then_rebuild_increases_chunks(tmp_path: Path) -> None:
    """追加一条 → rebuild → 块数比追加前多(至少 +1)。"""
    src = tmp_path / "knowledge"
    src.mkdir()
    (src / "base.md").write_text("# 基础\n\n基础内容。\n", encoding="utf-8")
    idx = KnowledgeIndex(
        source_dir=src,
        index_dir=tmp_path / "idx",
        offline=True,
        dim=32,
    )
    async def run() -> None:
        n_before = await idx.rebuild()
        idx.append_user_chunk("新补充的蓝牙规格信息。", title="蓝牙规格")
        n_after = await idx.rebuild()
        assert n_after == n_before + 1
        # 检索"蓝牙"应命中新加的块
        hits = await idx.query("蓝牙", top_k=3)
        titles = [h.title for h in hits]
        assert "蓝牙规格" in titles

    asyncio.run(run())


def test_append_multi_section_body(tmp_path: Path) -> None:
    """body 内含多个 `^# ` 行 → 切块器切成多个块,rebuild 块数应 +N。"""
    src = tmp_path / "knowledge"
    src.mkdir()
    (src / "base.md").write_text("# 基础\n\n基础内容。\n", encoding="utf-8")
    idx = KnowledgeIndex(
        source_dir=src,
        index_dir=tmp_path / "idx",
        offline=True,
        dim=32,
    )
    async def run() -> None:
        n_before = await idx.rebuild()
        # body 内含 2 个子标题 → 切块器切成 2 段(连同外层 title 共 3 段?不:外层 title 也作为第一段的标题)
        # 实际:_split_markdown 把 body 切成 2 段,但每段的 title 是 body 内的子标题
        # 整体:外层标题 + body 中 2 个子标题 → 3 块?需看实现
        # 简化:body 内 2 个 # 子标题 → 切块至少 +2
        body = "# 子标题1\n\n内容1\n\n# 子标题2\n\n内容2\n"
        idx.append_user_chunk(body, title="外层标题")
        n_after = await idx.rebuild()
        assert n_after >= n_before + 2

    asyncio.run(run())


# ──────────────────────────── list_chunks ────────────────────────────


def test_list_chunks_empty_when_no_index(tmp_path: Path) -> None:
    idx = KnowledgeIndex(
        source_dir=tmp_path / "knowledge",
        index_dir=tmp_path / "idx",
        offline=True,
        dim=8,
    )
    assert idx.list_chunks() == []


def test_list_chunks_returns_all(offline_idx: KnowledgeIndex) -> None:
    async def run() -> None:
        await offline_idx.rebuild()
        chunks = offline_idx.list_chunks()
        assert len(chunks) == 4
        # 字段齐全
        first = chunks[0]
        assert {"id", "title", "source", "body", "body_preview", "deletable"} <= set(first.keys())
        # 产品手册来源(intro.md/faq.md)不可删
        assert first["deletable"] is False

    asyncio.run(run())


def test_list_chunks_marks_user_knowledge_deletable(tmp_path: Path) -> None:
    src = tmp_path / "knowledge"
    src.mkdir()
    (src / "base.md").write_text("# 基础\n\n基础内容。\n", encoding="utf-8")
    idx = KnowledgeIndex(
        source_dir=src,
        index_dir=tmp_path / "idx",
        offline=True,
        dim=32,
    )

    async def run() -> None:
        idx.append_user_chunk("用户补充的蓝牙规格。", title="蓝牙规格")
        await idx.rebuild()
        chunks = idx.list_chunks()
        # 找到 user_knowledge.md 来源的块
        user_chunks = [c for c in chunks if c["source"] == "user_knowledge.md"]
        assert len(user_chunks) == 1
        assert user_chunks[0]["deletable"] is True
        assert user_chunks[0]["title"] == "蓝牙规格"
        assert "蓝牙规格" in user_chunks[0]["body"]

    asyncio.run(run())


# ──────────────────────────── delete_chunk ────────────────────────────


def test_delete_chunk_rejects_product_manual(tmp_path: Path) -> None:
    """产品手册块(source != user_knowledge.md)拒绝删除。"""
    src = tmp_path / "knowledge"
    src.mkdir()
    (src / "base.md").write_text("# 基础\n\n基础内容。\n", encoding="utf-8")
    idx = KnowledgeIndex(
        source_dir=src,
        index_dir=tmp_path / "idx",
        offline=True,
        dim=32,
    )

    async def run() -> None:
        await idx.rebuild()
        chunks = idx.list_chunks()
        manual_id = next(c["id"] for c in chunks if c["source"] == "base.md")
        ok = await idx.delete_chunk(manual_id)
        assert ok is False
        # 索引块数不变
        assert idx.doc_count == 1

    asyncio.run(run())


def test_delete_chunk_not_found(tmp_path: Path) -> None:
    src = tmp_path / "knowledge"
    src.mkdir()
    (src / "base.md").write_text("# 基础\n\n基础内容。\n", encoding="utf-8")
    idx = KnowledgeIndex(
        source_dir=src,
        index_dir=tmp_path / "idx",
        offline=True,
        dim=32,
    )

    async def run() -> None:
        await idx.rebuild()
        ok = await idx.delete_chunk(99999)
        assert ok is False

    asyncio.run(run())


def test_delete_chunk_user_knowledge_success(tmp_path: Path) -> None:
    """删除用户知识块:文件中对应段被移除,rebuild 后 doc_count -1。"""
    src = tmp_path / "knowledge"
    src.mkdir()
    (src / "base.md").write_text("# 基础\n\n基础内容。\n", encoding="utf-8")
    idx = KnowledgeIndex(
        source_dir=src,
        index_dir=tmp_path / "idx",
        offline=True,
        dim=32,
    )

    async def run() -> None:
        idx.append_user_chunk("蓝牙5.0可外接音箱", title="蓝牙规格")
        n_before = await idx.rebuild()
        chunks = idx.list_chunks()
        user_id = next(c["id"] for c in chunks if c["source"] == "user_knowledge.md")
        ok = await idx.delete_chunk(user_id)
        assert ok is True
        # rebuild 后块数 -1
        assert idx.doc_count == n_before - 1
        # user_knowledge.md 文件中不再有 # 蓝牙规格
        content = (src / "user_knowledge.md").read_text(encoding="utf-8")
        assert "# 蓝牙规格" not in content
        # list_chunks 也不再有
        chunks2 = idx.list_chunks()
        assert all(c["source"] != "user_knowledge.md" for c in chunks2)

    asyncio.run(run())


def test_delete_chunk_no_index_returns_false(tmp_path: Path) -> None:
    """未 rebuild 也无 db → delete 返回 False。"""
    idx = KnowledgeIndex(
        source_dir=tmp_path / "knowledge",
        index_dir=tmp_path / "idx",
        offline=True,
        dim=8,
    )

    async def run() -> None:
        ok = await idx.delete_chunk(0)
        assert ok is False

    asyncio.run(run())
