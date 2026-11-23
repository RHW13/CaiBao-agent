"""指标溯源证据链：为计算所用的每个科目找到 PDF 页码、原文与置信度。

匹配优先级：精确科目名 -> difflib 模糊匹配 -> LLM 语义匹配（可选）。
置信度低于 0.7 标记「需人工确认」。
"""
import difflib
from collections import OrderedDict
from functools import lru_cache

from .calibers import get_caliber
from .evaluator import parse_formula, FormulaError, iter_vars
from .mapping import clean_account, normalize_account
from .ratios import INDICATORS

LOW_CONFIDENCE = 0.7
_FUZZY_MIN = 0.6

# 年报三表内不存在、需用户外部输入的字段
EXTERNAL_INPUTS = {"每股市价", "每股股利", "折旧摊销"}

# LLM 匹配结果缓存：{(科目, 候选首行文本): (index, reason)}，按 LRU 淘汰
_LLM_CACHE_MAX = 256
_LLM_CACHE = OrderedDict()

_NO_MATCH = {
    "found": False,
    "page": None,
    "pages": [],
    "text": "",
    "values": [],
    "confidence": 0.0,
    "reason": "未在 PDF 中定位（不猜测数值）",
    "snippet": "",
    "flagged": True,
}


def _strip_suffix(name: str) -> str:
    for suf in ("_平均", "_上期"):
        if name.endswith(suf):
            return name[: -len(suf)]
    return name


def _formula_of(ind: dict) -> str:
    if ind.get("formula"):
        return ind["formula"]
    if ind.get("caliber"):
        return get_caliber(ind["caliber"], "标准")
    return ""


@lru_cache(maxsize=None)
def indicator_accounts(name: str) -> list:
    """返回某个指标公式引用的规范科目键（去重，按出现顺序）。"""
    for ind in INDICATORS:
        if ind["name"] != name:
            continue
        try:
            ast_node = parse_formula(_formula_of(ind))
        except FormulaError:
            return []
        out = []
        seen = set()
        for var in iter_vars(ast_node):
            base = _strip_suffix(var)
            if base and base not in seen:
                seen.add(base)
                out.append(base)
        return out
    return []


def _entry_result(entry: dict, confidence: float, reason: str, index) -> dict:
    snippet = index.context(entry["page"], entry["top"], entry["bottom"]) if index else ""
    return {
        "found": True,
        "page": entry["page"],
        "pages": entry["pages"],
        "text": entry["text"],
        "values": entry["values"],
        "confidence": confidence,
        "reason": reason,
        "snippet": snippet,
        "flagged": confidence < LOW_CONFIDENCE,
    }


def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, clean_account(a), clean_account(b)).ratio()


def _cache_put(key, value) -> None:
    """写入 LLM 匹配缓存；超出上限时淘汰最久未用项，避免长跑进程内存无界增长。"""
    _LLM_CACHE[key] = value
    _LLM_CACHE.move_to_end(key)
    while len(_LLM_CACHE) > _LLM_CACHE_MAX:
        _LLM_CACHE.popitem(last=False)


def _llm_pick(account: str, candidates: list):
    """让 LLM 在候选行中挑选最匹配的科目行，返回 (候选下标, 理由) 或 None。"""
    from . import config

    client = config.get_llm_client()
    if client is None or not candidates:
        return None
    key = (account, candidates[0]["text"])
    cached = _LLM_CACHE.get(key)
    if cached is not None:
        _LLM_CACHE.move_to_end(key)
        return cached

    listing = "\n".join(
        f"{i}. 第{c['page']}页：{c['text']}" for i, c in enumerate(candidates)
    )
    prompt = (
        f"下面是年报三表中的若干行。请判断哪一行最可能是科目「{account}」。\n"
        f"{listing}\n"
        '只输出 JSON：{"index": 行号, "reason": "一句话理由"}；若都不匹配则 index 为 -1。'
    )
    try:
        resp = client.chat.completions.create(
            model=config.get_llm_model(),
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
        )
        content = resp.choices[0].message.content or ""
        start, end = content.find("{"), content.rfind("}")
        if start == -1 or end <= start:
            return None
        import json

        data = json.loads(content[start : end + 1])
        idx = int(data.get("index", -1))
        reason = str(data.get("reason", "")).strip()
        result = (idx, reason)
        _cache_put(key, result)
        return result
    except Exception:
        return None


def find_evidence(index, account: str, use_llm: bool = True) -> dict:
    """查找某个科目在 PDF 中的证据。

    index 为 pdf_index.build_index 的返回值；account 为规范科目键。
    """
    if account in EXTERNAL_INPUTS:
        return {
            "found": False,
            "page": None,
            "pages": [],
            "text": "",
            "values": [],
            "confidence": 0.0,
            "reason": f"需外部输入（年报三表不含「{account}」）",
            "snippet": "",
            "flagged": False,
            "external": True,
        }

    exact = index.find(account)
    if exact:
        entry = exact[0]
        pages = "、".join(str(p) for p in entry["pages"])
        return _entry_result(entry, 1.0, f"科目名精确匹配（第 {pages} 页）", index)

    # 同名科目行存在但未进入数值表（报表空白项，按 0 计）
    for line in index.lines:
        if normalize_account(line.text) != account:
            continue
        entry = {
            "account": account,
            "page": line.page,
            "pages": [line.page],
            "text": line.text,
            "values": line.nums,
            "top": line.top,
            "bottom": line.bottom,
        }
        if line.nums:
            return _entry_result(
                entry, 0.9, f"科目行匹配（第 {line.page} 页，未进入主表数值）", index
            )
        return _entry_result(
            entry, 0.95, f"科目行存在但报表未填数值（第 {line.page} 页，计算时按 0 计）", index
        )

    # 模糊匹配：只在三表页且有数值的行中找
    pool = [ln for ln in index.lines if ln.nums and ln.text]
    scored = sorted(
        ((_similarity(account, ln.text), ln) for ln in pool),
        key=lambda x: x[0],
        reverse=True,
    )
    if scored and scored[0][0] >= _FUZZY_MIN:
        score, line = scored[0]
        confidence = min(0.9, max(_FUZZY_MIN, score * 0.9))
        entry = {
            "account": account,
            "page": line.page,
            "pages": [line.page],
            "text": line.text,
            "values": line.nums,
            "top": line.top,
            "bottom": line.bottom,
        }
        return _entry_result(
            entry, confidence, f"模糊匹配「{line.text}」（相似度 {score:.2f}）", index
        )

    if use_llm:
        candidates = []
        seen = set()
        for _, ln in scored[:20]:
            if ln.text in seen:
                continue
            seen.add(ln.text)
            candidates.append({"page": ln.page, "text": ln.text})
        picked = _llm_pick(account, candidates)
        if picked:
            idx, reason = picked
            if 0 <= idx < len(candidates):
                c = candidates[idx]
                line = next(
                    (ln for ln in pool if ln.page == c["page"] and ln.text == c["text"]), None
                )
                if line is not None:
                    entry = {
                        "account": account,
                        "page": line.page,
                        "pages": [line.page],
                        "text": line.text,
                        "values": line.nums,
                        "top": line.top,
                        "bottom": line.bottom,
                    }
                    return _entry_result(
                        entry, 0.75, f"LLM 语义匹配：{reason or '按语义判断'}", index
                    )

    return dict(_NO_MATCH)


def indicator_evidence(index, indicator: str, use_llm: bool = True) -> list:
    """返回某指标涉及的全部科目的证据列表。"""
    rows = []
    for account in indicator_accounts(indicator):
        ev = find_evidence(index, account, use_llm=use_llm)
        rows.append({"科目": account, **ev})
    return rows


def attach_evidence(index, ratios_df, use_llm: bool = False):
    """为比率表追加「溯源」与「置信」两列（页码摘要 + 需人工确认标记）。"""
    pages_col = []
    flag_col = []
    for name in ratios_df["指标"]:
        evs = indicator_evidence(index, name, use_llm=use_llm)
        found = [e for e in evs if e["found"]]
        pages = sorted({p for e in found for p in e["pages"]})
        low = [e["科目"] for e in evs if e["found"] and e["flagged"]]
        miss = [e["科目"] for e in evs if not e["found"] and not e.get("external")]
        ext = [e["科目"] for e in evs if e.get("external")]
        if pages:
            pages_col.append("、".join(f"P{p}" for p in pages))
        else:
            pages_col.append("需外部输入" if ext else "未定位")
        if miss:
            flag_col.append("未定位：" + "、".join(miss))
        elif low:
            flag_col.append("需人工确认：" + "、".join(low))
        elif ext:
            flag_col.append("需外部输入：" + "、".join(ext))
        else:
            flag_col.append("")
    out = ratios_df.copy()
    out["溯源"] = pages_col
    out["置信"] = flag_col
    return out
