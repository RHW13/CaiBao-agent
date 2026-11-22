"""附注风险扫描：规则定级 + LLM 结构化抽取（强制原文引用）。

6 类信号：审计意见 / 关键审计事项 / 会计政策变更 / 关联交易 / 商誉 / 股权质押。
规则通道始终运行；LLM 通道需显式开启，输出必须能在对应页码原文中逐字找到，
否则丢弃（无引用不输出）。
"""
import json
import re
from collections import OrderedDict

from . import config
from .notes_scan import build_notes
from .ratios import build_data

LEVELS = {"高危": 3, "中高": 2, "关注": 1, "提示": 0}

# 6 类信号的检索关键词
TYPE_KEYWORDS = {
    "审计意见": ["审计意见类型", "保留意见", "否定意见", "无法表示意见", "强调事项"],
    "关键审计事项": ["关键审计事项"],
    "会计政策变更": ["会计政策变更", "会计估计变更"],
    "关联交易": ["关联交易", "关联方交易", "资金占用", "对外担保"],
    "商誉": ["商誉"],
    "股权质押": ["股权质押", "股份质押", "质押股份"],
}

_OPINION_RE = re.compile(
    r"审计意见类型(标准的无保留意见|带强调事项段的无保留意见|保留意见|否定意见|无法表示意见)"
)
# 权益变动表中的固定行 / 交叉引用 / 明确“无变更”的表述，不算政策变更
_POLICY_NOISE = (
    "加：会计政策变更", "本期无", "无重大", "提示：无", "无会计政策",
    "见本附注", "详见", "未发生",
)
_PLEDGE_KWS = ["股权质押", "股份质押", "质押股份"]
_RELATED_RISK_KWS = ["资金占用", "对外担保", "拆借", "非经营性"]
# 关联交易章节的强特征（排除公司治理章节的泛泛提及）
_RELATED_STRONG_KWS = ["关联方关系", "关联交易类型", "关联交易情况", "关联方名称", "交易金额"]

# LLM 抽取缓存：{(信号类型, 原文片段): [条目]}，按 LRU 淘汰，避免长期运行内存无界增长
_CACHE_MAX = 64
_CACHE = OrderedDict()


def _cache_put(key, value) -> None:
    """写入 LLM 抽取缓存；超出上限时淘汰最久未用项。"""
    _CACHE[key] = value
    _CACHE.move_to_end(key)
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)


def _sig(type_, level, basis, page=None, quote="", source="规则"):
    return {"类型": type_, "等级": level, "依据": basis,
            "页码": page, "原文": quote, "来源": source}


def _amt(x) -> str:
    return "—" if x is None else f"{x / 1e8:.2f} 亿元"


def _norm(s: str) -> str:
    return re.sub(r"[\s\u3000]", "", s or "")


def _not_applicable(snippet: str, keyword: str) -> bool:
    """关键词后紧跟“不适用”且无可勾选标记，视为该项不适用。"""
    i = snippet.find(keyword)
    tail = snippet[i: i + 80] if i >= 0 else snippet
    return "不适用" in tail and "√" not in tail and "☑" not in tail


def _audit_signal(notes):
    """审计意见：标准无保留=提示；强调事项=关注；非标=高危。"""
    for hit in notes.search(["审计意见类型"], limit=5):
        m = _OPINION_RE.search(notes.page_text(hit["page"]))
        if not m:
            continue
        opinion = m.group(1)
        if opinion.startswith("标准"):
            level = "提示"
        elif "强调" in opinion:
            level = "关注"
        else:
            level = "高危"
        return _sig("审计意见", level, f"审计意见类型：{opinion}", hit["page"], hit["snippet"])
    return None


def _kam_signal(notes):
    hits = notes.search(["关键审计事项"], sections=["审计报告"], limit=1)
    if not hits:
        return None
    h = hits[0]
    return _sig("关键审计事项", "提示", "已披露关键审计事项，需关注审计师重点关注领域",
                h["page"], h["snippet"])


def _policy_signal(notes):
    for h in notes.search(["会计政策变更", "会计估计变更"], limit=5):
        if any(noise in h["snippet"] for noise in _POLICY_NOISE):
            continue
        if _not_applicable(h["snippet"], "会计政策变更"):
            continue
        return _sig("会计政策变更", "关注", "存在会计政策/会计估计变更，需核对追溯调整影响",
                    h["page"], h["snippet"])
    return None


def _related_signal(notes):
    hits = notes.search(["关联交易", "关联方交易"], limit=8)
    strong = [h for h in hits if any(k in h["snippet"] for k in _RELATED_STRONG_KWS)]
    if not strong:
        return None
    risky = next((h for h in strong if any(k in h["snippet"] for k in _RELATED_RISK_KWS)), None)
    hit = risky or strong[0]
    level = "关注" if risky else "提示"
    return _sig("关联交易", level, "关注关联交易定价公允性与资金占用/担保风险",
                hit["page"], hit["snippet"])


def _goodwill_signal(data, index, notes):
    goodwill = data.get("商誉")
    equity = data.get("所有者权益合计")
    if not goodwill or not equity:
        return None
    ratio = goodwill / equity
    if ratio > 0.5:
        level = "高危"
    elif ratio > 0.3:
        level = "中高"
    elif ratio > 0.1:
        level = "关注"
    else:
        level = "提示"
    page, quote = None, ""
    if index is not None:
        rows = index.find("商誉")
        if rows:
            page, quote = rows[0]["page"], rows[0]["text"]
    if not quote:
        hits = notes.search(["商誉"], limit=1)
        if hits:
            page, quote = hits[0]["page"], hits[0]["snippet"]
    return _sig("商誉", level, f"商誉 {_amt(goodwill)} / 净资产 {_amt(equity)} = {ratio:.1%}",
                page, quote)


def _pledge_signal(notes):
    for h in notes.search(_PLEDGE_KWS, limit=3):
        if "质押给本公司" in h["snippet"] or "质押给公司" in h["snippet"]:
            continue  # 债务人向本公司质押，属于增信而非本公司股东质押
        keyword = next((k for k in _PLEDGE_KWS if k in h["snippet"]), _PLEDGE_KWS[0])
        if _not_applicable(h["snippet"], keyword):
            continue
        return _sig("股权质押", "关注", "存在股权质押相关披露，需核对质押比例与平仓风险",
                    h["page"], h["snippet"])
    return None


_SYS_PROMPT = (
    "你是财报附注风险审阅助手。根据给定信号类型和原文片段，抽取相关披露事实，输出 JSON 数组。\n"
    '每个元素格式：{"类型": "...", "等级": "高危|中高|关注|提示", '
    '"原文": "逐字引用的原文句子", "页码": 数字}。\n'
    "类型限定：审计意见 / 关键审计事项 / 会计政策变更 / 关联交易 / 商誉 / 股权质押。\n"
    "即使披露结论为“无 / 不适用 / 标准无保留意见”，也抽取一条并把等级填为“提示”；"
    "只有片段与该类型完全无关时才输出 []。\n"
    "等级参考：非标审计意见、资金占用、大额商誉减值=高危；强调事项、政策变更、股权质押=关注；"
    "正常披露=提示。\n"
    "原文必须逐字出现在对应页码的片段中，不得改写或编造。只输出 JSON。"
)


def llm_signals(notes, per_type: int = 2):
    """LLM 抽取风险信号；逐条校验原文引用，未通过校验的丢弃。"""
    client = config.get_llm_client()
    if client is None:
        return [], "未配置 API Key，已跳过 LLM 抽取（规则通道结果不受影响）。"

    out = []
    for type_, keywords in TYPE_KEYWORDS.items():
        hits = notes.search(keywords, limit=per_type)
        if not hits:
            continue
        block = "\n".join(f"[第{h['page']}页] {h['snippet']}" for h in hits)
        key = (type_, block)
        if key not in _CACHE:
            value = []
            try:
                resp = client.chat.completions.create(
                    model=config.get_llm_model(),
                    messages=[
                        {"role": "system", "content": _SYS_PROMPT},
                        {"role": "user", "content": f"信号类型：{type_}\n原文片段：\n{block}"},
                    ],
                    temperature=0,
                )
                content = resp.choices[0].message.content or ""
                start, end = content.find("["), content.rfind("]")
                if start >= 0:
                    parsed = json.loads(content[start:end + 1])
                    if isinstance(parsed, list):
                        value = parsed
            except Exception:
                value = []
            _cache_put(key, value)
        else:
            _CACHE.move_to_end(key)

        for item in _CACHE[key]:
            if not isinstance(item, dict):
                continue  # 模型输出元素类型不符时跳过，避免结构异常中断扫描
            quote = str(item.get("原文", "")).strip()
            try:
                page = int(item.get("页码"))
            except (TypeError, ValueError):
                continue
            if not quote or _norm(quote) not in _norm(notes.page_text(page)):
                continue  # 原文无法验证，丢弃
            level = item.get("等级") if item.get("等级") in LEVELS else "关注"
            out.append(_sig(type_, level, "LLM 抽取（原文已校验）", page, quote, "LLM"))
    return out, ""


def scan_risks(pdf_data: dict, index=None, use_llm: bool = False) -> dict:
    """扫描 6 类风险信号，返回 {signals, checked, llm_note}。"""
    notes = build_notes(pdf_data.get("pages") or [])
    data = build_data(pdf_data)

    signals = [
        s for s in (
            _audit_signal(notes),
            _kam_signal(notes),
            _policy_signal(notes),
            _related_signal(notes),
            _goodwill_signal(data, index, notes),
            _pledge_signal(notes),
        ) if s
    ]

    note = ""
    if use_llm:
        rows, note = llm_signals(notes)
        seen = {(s["类型"], _norm(s["原文"])) for s in signals}
        for s in rows:
            key = (s["类型"], _norm(s["原文"]))
            if key not in seen:
                signals.append(s)
                seen.add(key)

    signals.sort(key=lambda s: -LEVELS.get(s["等级"], 0))
    return {"signals": signals, "checked": list(TYPE_KEYWORDS), "llm_note": note}
