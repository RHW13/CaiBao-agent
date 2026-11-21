"""附注章节切分与关键词检索（轻量检索，替代向量库）。

年报附注篇幅大但结构规整：先按标题关键词把页面归入章节，再按关键词命中
次数打分返回页码与原文片段。全流程确定性，不引入 chromadb / 向量模型依赖。
"""
from dataclasses import dataclass

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


def build_notes(pages: list) -> NotesIndex:
    """由 pdf_loader.extract_pages 的页面列表构建附注索引。"""
    sections = {name: [] for name in SECTIONS}
    by_page = {}
    for pg in pages:
        tags = [name for name, kws in SECTIONS.items() if any(k in pg["text"] for k in kws)]
        if tags:
            by_page[pg["page"]] = tags
            for name in tags:
                sections[name].append(pg["page"])
    return NotesIndex(pages=pages, sections=sections, by_page=by_page)
