"""附注章节切分与关键词检索（轻量检索，替代向量库）。

年报附注篇幅大但结构规整：先按标题关键词把页面归入章节，再按关键词命中
次数打分返回页码与原文片段。全流程确定性，不引入 chromadb / 向量模型依赖。
"""
import hashlib
import math
from collections import OrderedDict
from dataclasses import dataclass, field

# 章节 -> 标题关键词（页面命中任一即归档到该章节）
SECTIONS = {
    "管理层讨论与分析": ["管理层讨论与分析", "经营情况讨论与分析", "董事会报告"],
    "风险因素": ["风险因素", "可能面对的风险", "风险提示"],
    "审计报告": ["审计报告", "审计意见", "关键审计事项"],
    "会计政策": ["重要会计政策", "会计政策", "会计估计"],
    "合并范围": ["合并范围的变更", "在其他主体中的权益"],
    "关联方": ["关联方", "关联交易"],
    "或有事项": ["或有事项", "承诺事项"],
    "资产负债表日后事项": ["资产负债表日后事项"],
    "商誉": ["商誉"],
    "股份支付与质押": ["股份支付", "股权质押", "股份质押"],
    "分部报告": ["分部报告", "分部信息"],
}

_PAD = 90  # 原文片段前后保留的字符数


@dataclass
class NotesIndex:
    pages: list
    sections: dict   # 章节 -> [页码]
    by_page: dict    # 页码 -> [章节]
    avgdl: float = 1.0
    n_docs: int = 0
    df_cache: dict = field(default_factory=dict)   # 词 -> 出现页数（按需计算并缓存）

    def _df(self, keyword: str) -> int:
        """词出现在多少页（IDF 用），按需统计并缓存，避免全词表预扫。"""
        if keyword not in self.df_cache:
            self.df_cache[keyword] = sum(
                1 for pg in self.pages if keyword in pg.get("text", ""))
        return self.df_cache[keyword]

    def page_text(self, page: int) -> str:
        for pg in self.pages:
            if pg["page"] == page:
                return pg["text"]
        return ""

    @staticmethod
    def _snippet(text: str, keywords: list, pad: int = _PAD) -> str:
        for k in keywords:
            i = text.find(k)
            if i >= 0:
                return text[max(0, i - pad): i + len(k) + pad]
        return text[: pad * 2]

    def search(self, keywords: list, sections=None, limit: int = 5, pad: int = _PAD) -> list:
        """在指定章节（默认全本）中检索，按命中次数降序返回片段。"""
        wanted = set(sections) if sections else None
        hits = []
        for pg in self.pages:
            tags = self.by_page.get(pg["page"], [])
            if wanted and not wanted.intersection(tags):
                continue
            text = pg["text"]
            score = sum(text.count(k) for k in keywords)
            if score:
                hits.append({
                    "page": pg["page"],
                    "score": score,
                    "sections": tags,
                    "snippet": self._snippet(text, keywords, pad),
                })
        hits.sort(key=lambda r: -r["score"])
        return hits[:limit]

    def search_ranked(self, keywords: list, sections=None, limit: int = 5,
                      pad: int = _PAD, phrases=None, phrase_bonus: float = 1.5) -> list:
        """BM25 排序检索：IDF × 词频饱和 × 长度归一，另加短语命中加分。

        与 search() 返回结构一致，供问答使用；search() 保持原样以兼容风险扫描。
        """
        wanted = set(sections) if sections else None
        n = self.n_docs or len(self.pages) or 1
        avgdl = self.avgdl or 1.0
        k1, b = 1.2, 0.75
        hits = []
        for pg in self.pages:
            tags = self.by_page.get(pg["page"], [])
            if wanted and not wanted.intersection(tags):
                continue
            text = pg.get("text", "")
            dl = len(text) or 1
            score = 0.0
            for k in keywords:
                tf = text.count(k)
                if not tf:
                    continue
                df = self._df(k)
                idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
                score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * dl / avgdl))
            for p in (phrases or []):
                if p and p in text:
                    score += phrase_bonus
            if score > 0:
                hits.append({
                    "page": pg["page"],
                    "score": round(score, 6),
                    "sections": tags,
                    "snippet": self._snippet(text, list(keywords) + list(phrases or []), pad),
                })
        hits.sort(key=lambda r: -r["score"])
        return hits[:limit]


_NOTES_CACHE: "OrderedDict[tuple, NotesIndex]" = OrderedDict()
_NOTES_CACHE_MAX = 8


def pages_fingerprint(pages: list) -> tuple:
    """页面列表的内容指纹：首尾页页码、总字数与首尾页文本摘要。

    仅几十微秒开销，既可命中同一文档的重复构建，又不会像 id() 那样在对象
    释放后被复用，从而避免把 A 文档的附注索引错配给 B 文档。
    """
    if not pages:
        return (0,)
    first, last = pages[0], pages[-1]
    total = sum(len(p.get("text", "")) for p in pages)
    digest = hashlib.md5(
        (first.get("text", "") + "\x00" + last.get("text", "")).encode("utf-8", "ignore")
    ).hexdigest()
    return (len(pages), first.get("page"), last.get("page"), total, digest)


_pages_fingerprint = pages_fingerprint   # 兼容内部旧引用


def build_notes(pages: list) -> NotesIndex:
    """由 pdf_loader.extract_pages 的页面列表构建附注索引。

    同一份页面列表会被问答、风险扫描、归因多处复用，按内容指纹缓存结果，
    避免每次提问都全量重扫章节。
    """
    key = pages_fingerprint(pages)
    cached = _NOTES_CACHE.get(key)
    if cached is not None:
        _NOTES_CACHE.move_to_end(key)
        return cached

    sections = {name: [] for name in SECTIONS}
    by_page = {}
    for pg in pages:
        tags = [name for name, kws in SECTIONS.items() if any(k in pg["text"] for k in kws)]
        if tags:
            by_page[pg["page"]] = tags
            for name in tags:
                sections[name].append(pg["page"])
    total_chars = sum(len(p.get("text", "")) for p in pages)
    notes = NotesIndex(
        pages=pages, sections=sections, by_page=by_page,
        avgdl=(total_chars / len(pages)) if pages else 1.0,
        n_docs=len(pages),
    )
    _NOTES_CACHE[key] = notes
    while len(_NOTES_CACHE) > _NOTES_CACHE_MAX:
        _NOTES_CACHE.popitem(last=False)
    return notes
