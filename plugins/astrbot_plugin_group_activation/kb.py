"""L4 群公共知识库：把群里沉淀的「非个人」长期知识写进 AstrBot 知识库。

和 L3 的分工（为什么两套都要）：
- L3（memory.py）：**关于人**的结构化事实 —— 要按人精确查、要覆盖/遗忘、
  要按群隔离 → 结构化 JSON KV。
- 本模块：**关于群本身**的知识（梗、约定、共同经历、话题结论）——
  要按语义查（提问措辞千变万化，精确匹配没意义）→ FAISS 向量检索。

为什么检索不走 AstrBot 的 `kb_agentic_mode` 工具模式：那会让每条回复
多一轮「模型决定调工具 → 工具返回 → 模型总结」的往返（实测约 +3s）。
改为在 on_llm_request 里直接调 kb_manager.retrieve() 注入 —— 与 L1/L3
同款模式，延迟增量只有一次本地嵌入（bge-m3，几十 ms）。

写入走 upload_document(pre_chunked_text=...)：一条知识 = 一个文档 =
一个 chunk，不经过文件解析器。检索结果带 score 字段，写入前用它查重，
避免同一件事反复入库把库撑爆。
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

from astrbot.api import logger


class KBStore:
    """AstrBot 知识库的插件侧封装：每群一个库，自动建库、查重写入、检索注入。"""

    def __init__(self, context, cfg, data_dir: Path) -> None:
        self.context = context  # astrbot Context，其上有 kb_manager
        self.cfg = cfg
        self._usage_path = Path(data_dir) / "kb_usage.json"
        # 向量缓存：text → embedding。自己算余弦用（见 _embed 的说明）。
        self._emb_cache: dict[str, list[float]] = {}

    # ── 库管理 ────────────────────────────────────────
    def _kb_name(self, gid: str) -> str:
        return f"{self.cfg.kb_name_prefix}{gid}"

    async def _helper(self, gid: str):
        """拿本群知识库的 KBHelper；没有就建（惰性 —— 没知识流入就不建库）。"""
        if not self.cfg.kb_enable:
            return None
        mgr = self.context.kb_manager
        if mgr is None:
            logger.warning("[group_activation] context.kb_manager 不可用")
            return None
        name = self._kb_name(gid)
        helper = await mgr.get_kb_by_name(name)
        if helper is None:
            helper = await mgr.create_kb(
                name,
                description=f"群 {gid} 自动沉淀的长期知识（group_activation 插件维护）",
                embedding_provider_id=self.cfg.kb_embedding_provider_id,
            )
            logger.info("[group_activation] 已创建知识库「%s」", name)
        return helper

    # ── 写入（自动学习）──────────────────────────────
    async def remember(self, gid: str, texts: list[str]) -> tuple[int, str]:
        """把一批知识写进本群知识库。带语义查重与每日额度。

        返回 (实际写入条数, 说明文本)。说明文本给「手动写入」命令直接展示 ——
        之前失败一律回「没有写入（可能是额度用完、内容重复或写入失败）」，
        用户无从判断（实测把「同义重写被查重跳过」误当成功能坏了）。
        """
        texts = [t.strip() for t in (texts or []) if t and t.strip()]
        if not texts:
            return 0, "内容为空"
        if not self.cfg.kb_enable:
            return 0, "知识库功能已关闭（kb_enable=false）"
        helper = await self._helper(gid)
        if helper is None:
            return 0, "知识库不可用（kb_manager 未就绪或建库失败）"
        added = dup = failed = 0
        dup_score = 0.0
        quota_hit = False
        for t in texts:
            if added >= self.cfg.kb_daily_limit or not self._quota_left():
                quota_hit = True
                logger.info("[group_activation] 知识库今日写入额度用完（群 %s）", gid)
                break
            try:
                score = await self._dup_score(helper, t)
                if score >= self.cfg.kb_dedup_score:
                    dup += 1
                    dup_score = max(dup_score, score)
                    logger.info(
                        "[group_activation] 知识库查重跳过（群 %s，相似度 %.2f）：%s",
                        gid,
                        score,
                        t[:40],
                    )
                    continue
                # 时间戳直接拼进正文（而不是存别处）：嵌入时一起进去，
                # 检索命中后模型能看到「这条知识是什么时候记的」——
                # 久远的条目（如「最近大家在追 X」）可以自行打折扣。
                stamped = (
                    f"（记于 {time.strftime('%Y-%m-%d')}）"
                    f"{t[: self.cfg.kb_max_chars]}"
                )
                await helper.upload_document(
                    file_name=f"ga-{gid}-{int(time.time() * 1000)}.txt",
                    file_content=None,
                    file_type="txt",
                    pre_chunked_text=[stamped],
                )
                self._quota_count_up()
                added += 1
            except Exception as e:  # noqa: BLE001
                failed += 1
                logger.warning("[group_activation] 写入知识库失败（群 %s）: %s", gid, e)
        return added, self._explain(added, dup, failed, quota_hit, dup_score)

    @staticmethod
    def _explain(
        added: int, dup: int, failed: int, quota_hit: bool, dup_score: float
    ) -> str:
        """把写入结果翻译成人话（给 /garemember 直接回复用）。"""
        if added:
            return f"已记住 {added} 条。🫧"
        if quota_hit:
            return "没写入：今日写入额度已用完（明天自动恢复，或调大 kb_daily_limit）"
        if dup:
            return (
                f"没写入：这条库里已经有了（与已有知识相似度 {dup_score:.0%}）"
                "—— 换个更具体的说法可以强制记入"
            )
        if failed:
            return "没写入：写入过程报错，看容器日志里的 [group_activation] 报错行"
        return "没写入：未知原因"

    # ── 自己算余弦（绕开 AstrBot 的分数体系）────────────
    # 为什么必须自己算：AstrBot 的两套分数都不能和固定阈值比较 ——
    #   ① 底层 vec_db 的原始分是 `1 - L2²/2`，而 Ollama 的 bge-m3 向量**未归一化**
    #      （实测模长 25.7），距离是几千的量级 → 原始分是负几百；
    #   ② 融合后的分是 min-max **相对分**（rank_fusion），库里只有一两条时
    #      任何查询都会被归一化成接近 1.0 → 查重必中、注入门槛形同虚设
    #      （2026-10-08 实测：手动 /garemember 全被判重复，回「没有写入」）。
    # 自己问同一个 Ollama 端点要向量、自己算余弦，才是稳定可比的（按文本缓存）。
    async def _embed(self, text: str) -> list[float] | None:
        key = (text or "").strip()
        if not key:
            return None
        hit = self._emb_cache.get(key)
        if hit is not None:
            return hit
        base, model = self._embed_endpoint()
        if not base or not model:
            logger.warning("[group_activation] 拿不到嵌入端点配置，无法自算相似度")
            return None
        try:
            import aiohttp

            url = base.rstrip("/") + "/api/embeddings"
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.post(url, json={"model": model, "prompt": key}) as resp:
                    data = await resp.json()
            vec = data.get("embedding")
        except Exception as e:  # noqa: BLE001
            logger.warning("[group_activation] 嵌入调用失败: %s", e)
            return None
        if not isinstance(vec, list) or not vec:
            return None
        if len(self._emb_cache) > 5000:  # 简单容量保护
            self._emb_cache.clear()
        self._emb_cache[key] = vec
        return vec

    def _embed_endpoint(self) -> tuple[str, str]:
        """从 provider 配置里取嵌入端点与模型名（merged=True 会带上 source 字段）。"""
        try:
            cfg = (
                self.context.provider_manager.get_provider_config_by_id(
                    self.cfg.kb_embedding_provider_id, merged=True
                )
                or {}
            )
            return (
                str(cfg.get("embedding_api_base") or ""),
                str(cfg.get("embedding_model") or ""),
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("[group_activation] 读嵌入端点配置失败: %s", e)
            return "", ""

    @staticmethod
    def _cos(a: list[float], b: list[float]) -> float:
        num = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        return num / (na * nb) if na and nb else 0.0

    async def similarity(self, a: str, b: str) -> float:
        """两条文本的余弦相似度（自算、带缓存）。L3 写入前的语义去重也用它。"""
        va = await self._embed(a)
        vb = await self._embed(b)
        if va is None or vb is None:
            return 0.0
        return self._cos(va, vb)

    async def _top_chunks(
        self, helper, text: str, k: int
    ) -> list[tuple[str, float]]:
        """取库里最相近的若干 chunk，返回 [(文本, 自算余弦)]，按相似度降序。

        chunk 的**召回**仍走底层向量库（快，只拿候选排名），**分数一律自算**
        —— 向量库的分不可比（见上面 _embed 的说明）。
        """
        vec_db = getattr(helper, "vec_db", None)
        if vec_db is None:
            try:
                vec_db = await helper._ensure_vec_db()  # noqa: SLF001（无公开入口）
            except Exception as e:  # noqa: BLE001
                logger.warning("[group_activation] 初始化向量库失败: %s", e)
                return []
        try:
            results = await vec_db.retrieve(text, k=max(k, 3))
        except Exception as e:  # noqa: BLE001
            logger.warning("[group_activation] 向量检索失败: %s", e)
            return []
        qv = await self._embed(text)
        if qv is None:
            return []
        out: list[tuple[str, float]] = []
        for r in results:
            content = str((getattr(r, "data", None) or {}).get("text") or "").strip()
            if not content:
                continue
            sv = await self._embed(content)
            if sv is None:
                continue
            out.append((content, self._cos(qv, sv)))
        out.sort(key=lambda x: x[1], reverse=True)
        return out

    async def _dup_score(self, helper, text: str) -> float:
        """查重：候选文本与库里已有知识的最高余弦相似度（没命中返回 0）。

        阈值 0.90 是实测定下来的（2026-10-08，bge-m3，自算余弦）：
        「同一件事换个说法」0.92~0.93；「措辞相近的不同事」≤0.77
        （「小泽布尔是管理员」vs「…是群主」只有 0.77）。分界干净。
        """
        pairs = await self._top_chunks(helper, text, k=3)
        return pairs[0][1] if pairs else 0.0

    # ── 检索（自动注入）──────────────────────────────
    async def search(self, gid: str, query: str) -> str:
        """用当前消息检索本群知识库，返回注入文本；没有相关条目就返回空串。

        分数用**自算余弦**（见 _embed 的说明）—— 之前用 kb_manager.retrieve 的
        融合分，库里只有一条时任何消息都会被注入（0.35 门槛形同虚设）。
        """
        if not self.cfg.kb_enable or not (query or "").strip():
            return ""
        mgr = self.context.kb_manager
        if mgr is None:
            return ""
        helper = await mgr.get_kb_by_name(self._kb_name(gid))
        # 库不存在 / 还没内容：直接跳过，省一次嵌入调用
        if helper is None or helper.kb.doc_count == 0:
            return ""
        pairs = await self._top_chunks(
            helper, query.strip(), k=self.cfg.kb_inject_top * 3
        )
        lines = [
            f"- {text}"
            for text, score in pairs[: self.cfg.kb_inject_top]
            if score >= self.cfg.kb_min_score
        ]
        return "\n".join(lines)

    # ── 管理（调试命令用）────────────────────────────
    async def stats(self, gid: str) -> str:
        """本群知识库概况：总条数 + 最近文档名。只读，不会创建库。"""
        mgr = self.context.kb_manager
        helper = await mgr.get_kb_by_name(self._kb_name(gid)) if mgr else None
        if helper is None:
            return "本群还没有知识库（尚无沉淀的知识）。"
        count = await helper.count_documents()
        if count == 0:
            return "本群知识库还是空的（尚无沉淀的知识）。"
        docs = await helper.list_documents(limit=8)
        lines = [f"本群知识库共 {count} 条知识，最近 {len(docs)} 条："]
        for d in docs:
            name = getattr(d, "doc_name", "?")
            lines.append(f"- {name}")
        return "\n".join(lines)

    async def forget(self, gid: str) -> int:
        """清空本群知识库的所有文档（保留库本身，额度计数不清）。"""
        mgr = self.context.kb_manager
        helper = await mgr.get_kb_by_name(self._kb_name(gid)) if mgr else None
        if helper is None:
            return 0
        docs = await helper.list_documents(limit=10000)
        n = 0
        for d in docs:
            doc_id = getattr(d, "doc_id", None)
            if not doc_id:
                continue
            try:
                await helper.delete_document(doc_id)
                n += 1
            except Exception as e:  # noqa: BLE001
                logger.warning("[group_activation] 删除知识文档失败: %s", e)
        return n

    # ── 每日额度（与联网搜索同款模式）────────────────
    def _quota_left(self) -> int:
        today = time.strftime("%Y-%m-%d")
        try:
            with open(self._usage_path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:  # noqa: BLE001
            return self.cfg.kb_daily_limit
        if data.get("date") != today:
            return self.cfg.kb_daily_limit
        try:
            used = int(data.get("count", 0))
        except (TypeError, ValueError):
            used = 0
        return max(0, self.cfg.kb_daily_limit - used)

    def _quota_count_up(self) -> None:
        """写入成功后计数 +1（原子写，避免写坏计数文件）。"""
        today = time.strftime("%Y-%m-%d")
        try:
            path = self._usage_path
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:  # noqa: BLE001
                data = {}
            used = int(data.get("count", 0)) if data.get("date") == today else 0
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"date": today, "count": used + 1}, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(path)
        except Exception as e:  # noqa: BLE001
            logger.warning("[group_activation] 写知识库计数失败: %s", e)
