"""财报语义问答：数字题确定性计算，文本题检索 + 可选 LLM，强制引用。

优化要点：
- BM25 排序 + 短语加分，关键词去冗余（子串归并）；
- 比率/科目结果按文档指纹缓存，避免逐题重算；
- 多轮指代继承、跨期（本期/上期）与跨公司同行对比；
- 统一返回 confidence / basis；非财报问题、空文档、歧义指标显式拦截；
- LLM 仅生成且带超时，校验失败回退原文摘录。
"""
import re
import unicodedata
from collections import OrderedDict

from . import config
from .attribution import dupont_attribution, gross_margin_breakdown, mdna_evidence
from .evidence import indicator_evidence
from .notes_scan import build_notes, pages_fingerprint
from .pdf_index import build_index
from .ratios import build_data, compute_ratios

_RATIO_ALIASES = {
    "流动比率": "流动比率", "速动比率": "速动比率", "现金比率": "现金比率",
    "资产负债率": "资产负债率", "产权比率": "产权比率", "权益乘数": "权益乘数",
    "毛利率": "销售毛利率", "销售毛利率": "销售毛利率",
    "净利率": "销售净利率", "净利润率": "销售净利率", "销售净利率": "销售净利率",
    "roe": "净资产收益率(ROE)", "净资产收益率": "净资产收益率(ROE)",
    "roa": "总资产净利率(ROA)", "总资产净利率": "总资产净利率(ROA)",
    "存货周转率": "存货周转率", "应收账款周转率": "应收账款周转率",
    "总资产周转率": "总资产周转率", "营业周期": "营业周期",
    "净现比": "净现比(盈余现金保障倍数)",
    "盈余现金保障倍数": "净现比(盈余现金保障倍数)",
    "每股收益": "每股收益(EPS)", "eps": "每股收益(EPS)",
    "市盈率": "市盈率(P/E)", "市净率": "市净率(P/B)", "市销率": "市销率(P/S)",
    "每股净资产": "每股净资产", "股息率": "股息率", "股利支付率": "股利支付率",
    "利息保障倍数": "利息保障倍数", "现金流量比率": "现金流量比率",
    "现金流量债务比": "现金流量债务比", "长期资本负债率": "长期资本负债率",
}

_ACCOUNT_ALIASES = {
    "营收": "营业收入", "收入": "营业收入", "净利": "净利润", "利润": "净利润",
    "经营现金流": "经营活动产生的现金流量净额",
    "现金流净额": "经营活动产生的现金流量净额",
    "总资产": "资产总计", "净资产": "所有者权益合计", "负债": "负债合计",
    "货币资金": "货币资金", "应收账款": "应收账款", "存货": "存货",
    "合同负债": "合同负债", "研发费用": "研发费用", "销售费用": "销售费用",
    "管理费用": "管理费用", "财务费用": "财务费用", "股本": "股本",
}

_NUM_HINTS = ("多少", "是多少", "金额", "余额", "数值", "计算", "同比",
              "增长", "增速", "增长率", "占比", "比例", "几")
_REASON_HINTS = ("为什么", "原因", "为何", "说明", "解释", "分析",
                 "风险", "情况", "如何", "是否")
_STOPWORDS = ("公司", "年报", "报告", "请问", "请", "是多少", "什么", "为什么",
              "如何", "哪些", "情况", "原因", "说明", "一下", "相关", "关于",
              "是否", "以及", "的", "吗", "呢", "和", "与", "在", "中", "对",
              "变化", "下降", "上升", "增长", "减少", "增加", "波动")

_PHRASE_HINTS = {
    "经营": ["经营情况", "经营业绩", "营业收入", "净利润"],
    "风险": ["风险因素", "风险"],
    "毛利": ["毛利率", "营业成本"],
    "成本": ["营业成本", "毛利率"],
    "现金": ["经营活动", "现金流", "回款"],
    "研发": ["研发投入", "研发费用"],
    "分红": ["利润分配", "现金分红"],
    "关联": ["关联交易", "关联方"],
}

# 判断问题是否与财报相关（用于拦截无关提问，避免给出“建议查阅附注”式误导）
_FIN_LEXICON = (
    "公司", "年报", "报告", "财务", "财报", "报表", "附注", "审计", "主营", "业务",
    "经营", "竞争", "市场", "份额", "计划", "战略", "风险", "收入", "营收", "利润",
    "净利", "资产", "负债", "权益", "现金", "毛利", "成本", "费用", "存货", "应收",
    "应付", "股东", "分红", "股利", "股息", "现金流", "周转", "比率", "收益率",
    "增速", "增长", "同比", "环比", "商誉", "研发", "投资", "融资", "偿债", "盈利",
    "营运", "估值", "每股", "市值", "市盈率", "市净率", "杜邦",
) + tuple(_RATIO_ALIASES) + tuple(_ACCOUNT_ALIASES) + tuple(set(_RATIO_ALIASES.values()))

_COMPARE_HINTS = ("对比", "相比", "比较", "同行", "排名", "最高", "最低", "哪家")
_FOLLOWUP_HINTS = ("那", "呢", "它", "其", "这个", "该", "上述", "那么", "相比")

_SYS_PROMPT = (
    "你是财报问答助手。只能依据下面提供的原文片段回答，不得使用片段外的知识。\n"
    "每个结论后必须标注来源页码，格式为 [第X页]。\n"
    "片段不足以回答时，直接回答“资料中未找到依据”，并说明可能查阅的章节。\n"
    "涉及数字时只引用片段中已有的数字，不要自行加减乘除。"
)

# ---------------- 结果缓存（按文档内容指纹，避免逐题重算） ----------------

_CACHE_MAX = 4
_ratio_cache: "OrderedDict[tuple, object]" = OrderedDict()
_data_cache: "OrderedDict[tuple, dict]" = OrderedDict()


def _cache_get(cache, key):
    if key in cache:
        cache.move_to_end(key)
        return cache[key]
    return None


def _cache_put(cache, key, value) -> None:
    cache[key] = value
    while len(cache) > _CACHE_MAX:
        cache.popitem(last=False)


def _fingerprint(pdf_data: dict) -> tuple:
    return pages_fingerprint(pdf_data.get("pages") or [])


def _ratio_table(pdf_data: dict, caliber):
    key = (_fingerprint(pdf_data), repr(caliber))
    cached = _cache_get(_ratio_cache, key)
    if cached is None:
        cached = compute_ratios(pdf_data, caliber=caliber)
        _cache_put(_ratio_cache, key, cached)
    return cached


def _flat_data(pdf_data: dict) -> dict:
    key = _fingerprint(pdf_data)
    cached = _cache_get(_data_cache, key)
    if cached is None:
        cached = build_data(pdf_data)
        _cache_put(_data_cache, key, cached)
    return cached


# ---------------- 基础工具 ----------------


def _norm(text: str) -> str:
    """全角转半角、去首尾空白、英文小写，统一后续匹配口径。"""
    return unicodedata.normalize("NFKC", text or "").strip().lower()


def _fmt_value(value, unit: str) -> str:
    if value is None:
        return "—"
    if unit == "%":
        return f"{value:.2f}%"
    if unit == "亿元":
        return f"{value:.2f} 亿元"
    if unit == "元/股":
        return f"{value:.4f} 元/股"
    return f"{value:.2f} {unit}"


def _is_numeric(q: str) -> bool:
    if any(h in q for h in _REASON_HINTS) and not any(h in q for h in ("是多少", "金额", "余额")):
        return False
    return any(h in q for h in _NUM_HINTS)


def _match_ratio_all(q: str) -> list:
    """返回问题命中的全部指标（长别名优先，短别名被长别名包含时忽略）。"""
    lower = _norm(q)
    matched = []
    for alias in sorted(_RATIO_ALIASES, key=len, reverse=True):
        if alias.lower() in lower and not any(alias in m for m in matched):
            matched.append(alias)
    return list(dict.fromkeys(_RATIO_ALIASES[a] for a in matched))


def _match_ratio(q: str):
    found = _match_ratio_all(q)
    return found[0] if found else None


def _account_terms(data: dict):
    terms = list(_ACCOUNT_ALIASES.items())
    terms += [(k, k) for k in data if not k.endswith(("_上期", "_平均"))]
    return sorted(terms, key=lambda item: -len(item[0]))


def _match_account(q: str, data: dict):
    for term, canonical in _account_terms(data):
        if term in q:
            return canonical
    return None


def _period_intent(q: str):
    """识别跨期询问：上期 / 同比 / 多年趋势。"""
    if any(w in q for w in ("近三年", "三年", "历年", "趋势", "近几年")):
        return "trend"
    if "同比" in q:
        return "both"
    if any(w in q for w in ("上期", "去年", "上年", "同期")):
        return "prev"
    return None


def _is_finance_related(q: str) -> bool:
    return any(w in q for w in _FIN_LEXICON)


# ---------------- 数字题 ----------------


def _answer_ratio(name: str, pdf_data: dict, index, question: str,
                  caliber="标准", period=None) -> dict:
    ratios = _ratio_table(pdf_data, caliber)
    row = ratios[ratios["指标"] == name]
    if row.empty:
        return {"route": "ratio", "answer": f"未找到指标「{name}」。", "citations": [],
                "confidence": 0.5, "basis": "指标不在当前口径列表中"}
    record = row.iloc[0]
    value = record["数值"]
    if value is None or value != value:
        reason = record["备注"] or "数据缺失"
        return {"route": "ratio", "answer": f"{name} 当前无法计算：{reason}。", "citations": [],
                "confidence": 0.6, "basis": "数据缺失，未给出数值"}

    citations = []
    seen = set()
    for item in indicator_evidence(index, name, use_llm=False):
        if not item["found"]:
            continue
        for page in item["pages"]:
            if page in seen:
                continue
            seen.add(page)
            citations.append({"page": page, "snippet": f"{item['科目']}：{item['text']}"})

    answer = f"{name} = {_fmt_value(value, record['单位'])}"
    if record["口径"] != "标准":
        answer += f"（{record['口径']}口径）"
    answer += "。"

    if period:
        margin = gross_margin_breakdown(pdf_data) if name == "销售毛利率" else None
        if margin and margin.get("ok"):
            prev = margin["gm_prev"]
            if period == "trend":
                answer += (f"当前仅解析两期：上期 {prev:.2%} → 本期 {margin['gm_cur']:.2%}，"
                           "无法给出连续三年趋势。")
            else:
                answer += f"上期毛利率 {prev:.2%}。"
        elif period == "trend":
            answer += "当前仅解析两期，无法给出连续多年趋势。"
        else:
            answer += "该比率仅提供本期值（历史比率未单独存储）。"

    if citations:
        answer += "关键科目见 " + "、".join(f"[第{c['page']}页]" for c in citations[:3]) + "。"
    return {"route": "ratio", "answer": answer, "citations": citations,
            "value": value, "unit": record["单位"],
            "confidence": 1.0, "basis": "确定性计算"}


def _answer_account(name: str, question: str, data: dict, index,
                    period=None, company: str = "") -> dict:
    current = data.get(name)
    if current is None:
        return {"route": "account", "answer": f"{name}：资料中未找到该科目。", "citations": [],
                "confidence": 0.5, "basis": "科目缺失"}
    previous = data.get(name + "_上期")
    growth = (current - previous) / abs(previous) if previous else None
    prefix = f"{company} " if company else ""

    parts = []
    if period == "prev":
        if previous is None:
            parts.append(f"{name}：资料中未提供上期数据")
        else:
            parts.append(f"{name}（上期）：{previous:,.2f} 元（约 {previous / 1e8:.2f} 亿元）")
            parts.append(f"本期 {current:,.2f} 元")
    else:
        parts.append(f"{prefix}{name}：{current:,.2f} 元（约 {current / 1e8:.2f} 亿元）")
        if previous is not None:
            parts.append(f"上期 {previous:,.2f} 元")
        if growth is not None:
            parts.append(f"同比 {growth:+.2%}")
        if period == "trend":
            parts.append("当前仅解析两期，无法给出连续多年趋势")
        if previous is None and "同比" in question:
            parts.append("上期数据缺失，无法计算同比")

    citations = []
    seen = set()
    for entry in (index.find(name) if index is not None else []):
        if entry["page"] in seen:
            continue
        seen.add(entry["page"])
        citations.append({"page": entry["page"], "snippet": entry["text"]})
        if len(citations) >= 3:
            break
    answer = "；".join(parts) + "。"
    if citations:
        answer += "来源 " + "、".join(f"[第{c['page']}页]" for c in citations) + "。"
    conf = 1.0 if previous is not None or period != "prev" else 0.9
    return {"route": "account", "answer": answer, "citations": citations, "value": current,
            "confidence": conf, "basis": "确定性计算"}


# ---------------- 检索 ----------------


def _keywords(question: str) -> list:
    text = _norm(question)
    for word in _STOPWORDS:
        text = text.replace(word, " ")

    tokens = []
    for chunk in re.split(r"[^\u4e00-\u9fa5A-Za-z0-9]+", text):
        if not chunk:
            continue
        if len(chunk) <= 2:
            tokens.append(chunk)
        else:  # 中文无空格：用纯中文 2 字滑窗提升召回
            tokens.extend(chunk[i:i + 2] for i in range(len(chunk) - 1)
                          if re.fullmatch(r"[\u4e00-\u9fa5]{2}", chunk[i:i + 2]))

    for hint, phrases in _PHRASE_HINTS.items():
        if hint in text:
            tokens.extend(phrases)
    for alias, canonical in _RATIO_ALIASES.items():
        if alias.lower() in text:
            tokens.extend([alias, canonical])
    for alias, canonical in _ACCOUNT_ALIASES.items():
        if alias in text:
            tokens.extend([alias, canonical])

    cleaned = [t for t in tokens if len(t) >= 2 and t not in _STOPWORDS]
    uniq = sorted(dict.fromkeys(cleaned), key=len, reverse=True)
    # 子串去冗余：短词若被更长词包含则丢弃（“营业”/“收入”并入“营业收入”）
    kept = [t for t in uniq if not any(t != other and t in other for other in uniq)]
    return kept[:8]


def _retrieve_scored(question: str, pdf_data: dict, limit: int = 4):
    """返回 (citations, top_score)；分档检索命中即停。"""
    notes = build_notes(pdf_data.get("pages") or [])
    q = _norm(question)
    keywords = _keywords(question)
    if not keywords:
        return [], 0.0
    phrases = [a for a in _RATIO_ALIASES if a.lower() in q]
    phrases += [a for a in _ACCOUNT_ALIASES if a in q]

    starts = [p for p in (pdf_data.get("stmt_starts") or {}).values() if p]
    upper = min(starts) if starts else 10 ** 9

    if any(w in q for w in ("风险", "诉讼", "担保", "质押", "不确定")):
        sections = ["风险因素", "管理层讨论与分析"]
    elif any(w in q for w in ("经营", "业务", "原因", "为什么", "发展", "战略")):
        sections = ["管理层讨论与分析"]
    else:
        sections = None

    def in_range(hits):
        return [h for h in hits if 5 < h["page"] < upper]

    hits = in_range(notes.search_ranked(keywords, sections, limit * 3, phrases=phrases))
    if not hits and sections:
        hits = in_range(notes.search_ranked(keywords, None, limit * 3, phrases=phrases))
    if not hits:
        hits = notes.search_ranked(keywords, None, limit, phrases=phrases)

    top = hits[0]["score"] if hits else 0.0
    return [{"page": h["page"], "snippet": h["snippet"]} for h in hits[:limit]], top


def retrieve(question: str, pdf_data: dict, limit: int = 4) -> list:
    """公开检索入口（供流式问答复用）。"""
    return _retrieve_scored(question, pdf_data, limit)[0]


def _retrieve(question: str, pdf_data: dict, limit: int = 4) -> list:
    return retrieve(question, pdf_data, limit)


# ---------------- 答案校验 ----------------


_AMOUNT_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(亿元|亿|万元|万|%|元)?")


def _num_values(text: str) -> set:
    """抽取数值并归一化到元（亿/万乘算），百分比按原值比较。"""
    values = set()
    for raw, unit in _AMOUNT_RE.findall(text or ""):
        try:
            val = float(raw.replace(",", ""))
        except ValueError:
            continue
        if unit in ("亿", "亿元"):
            val *= 1e8
        elif unit in ("万", "万元"):
            val *= 1e4
        values.add(round(val, 4))
    return values


def _numbers_supported(answer: str, snippets: list) -> bool:
    """数字回查：答案中的数值必须能在引用原文中找到（允许 1% 单位换算误差）。"""
    body = re.sub(r"第\s*\d+\s*页", " ", answer or "")
    asked = _num_values(body)
    if not asked:
        return True
    allowed = _num_values(" ".join(snippets))
    for val in asked:
        if val in allowed:
            continue
        if any(abs(val - a) <= max(1e-6, abs(a) * 0.01) for a in allowed):
            continue
        return False
    return True


def validate_answer(answer: str, citations: list) -> str:
    """校验 LLM 答案：必须引用命中页码，且数字可回查；不通过返回空串。"""
    answer = (answer or "").strip()
    if not answer:
        return ""
    valid = {c["page"] for c in citations}
    cited = {int(p) for p in re.findall(r"第(\d+)页", answer)}
    if not (cited & valid):
        return ""
    if not _numbers_supported(answer, [c["snippet"] for c in citations]):
        return ""
    return answer


# ---------------- 生成 ----------------


def _llm_messages(question: str, block: str) -> list:
    return [
        {"role": "system", "content": _SYS_PROMPT},
        {"role": "user", "content": f"原文片段：\n{block}\n\n问题：{question}"},
    ]


def _llm_answer(question: str, citations: list) -> str:
    client = config.get_llm_client()
    if client is None:
        return ""
    block = "\n".join(f"[第{c['page']}页] {c['snippet']}" for c in citations)
    try:
        resp = client.chat.completions.create(
            model=config.get_llm_model(),
            messages=_llm_messages(question, block),
            temperature=0, timeout=12, max_tokens=600,
        )
        answer = (resp.choices[0].message.content or "").strip()
    except Exception:
        return ""
    return validate_answer(answer, citations)


def llm_stream(question: str, citations: list):
    """流式生成（不校验）；调用方应在结束后用 validate_answer 复核。"""
    client = config.get_llm_client()
    if client is None:
        return
    block = "\n".join(f"[第{c['page']}页] {c['snippet']}" for c in citations)
    try:
        stream = client.chat.completions.create(
            model=config.get_llm_model(),
            messages=_llm_messages(question, block),
            temperature=0, timeout=12, max_tokens=600, stream=True,
        )
        for chunk in stream:
            try:
                delta = chunk.choices[0].delta.content or ""
            except (IndexError, AttributeError):
                delta = ""
            if delta:
                yield delta
    except Exception:
        return


def _extractive(citations: list) -> str:
    lines = [f"- {c['snippet']} [第{c['page']}页]" for c in citations]
    return "资料中与问题相关的原文如下：\n" + "\n".join(lines)


def _citations_for(index, names: list, limit: int = 4) -> list:
    citations, seen = [], set()
    for name in names:
        for entry in index.find(name):
            if entry["page"] in seen:
                continue
            seen.add(entry["page"])
            citations.append({"page": entry["page"], "snippet": entry["text"]})
            break
    return citations[:limit]


# ---------------- 原因题 ----------------


def _answer_attribution(question: str, pdf_data: dict, index):
    """原因类问题：先给系统计算推断，再附公司解释原文。"""
    q = _norm(question)
    if "毛利率" in q:
        margin = gross_margin_breakdown(pdf_data)
        if not margin["ok"]:
            return None
        effects = {r["项目"]: r["影响"] for r in margin["rows"]}
        parts = [
            f"计算推断：毛利率 {margin['gm_prev']:.2%} → {margin['gm_cur']:.2%}"
            f"（{margin['change']:+.2%}）。",
            f"按连环替代拆分：收入影响 {effects['收入影响']:+.2%}，"
            f"成本影响 {effects['成本影响']:+.2%}。",
        ]
        citations = _citations_for(index, ["营业收入", "营业成本"])
        evidence = mdna_evidence(
            pdf_data, keywords=["毛利率", "营业成本", "成本", "价格", "原材料"], limit=2)
        if evidence:
            parts.append("公司解释（原文摘录）：" + " ".join(
                f"{e['snippet'].lstrip('。；，、 ')} [第{e['page']}页]" for e in evidence))
            citations += [{"page": e["page"], "snippet": e["snippet"]} for e in evidence]
        answer = " ".join(parts)
        if citations:
            answer += " 来源 " + "、".join(f"[第{c['page']}页]" for c in citations[:4]) + "。"
        return {"route": "attribution", "answer": answer, "citations": citations,
                "confidence": 0.9, "basis": "计算推断 + 原文摘录"}

    if "roe" in q or "净资产收益率" in q:
        attr = dupont_attribution(pdf_data)
        if not attr["ok"]:
            return None
        parts = [
            f"计算推断：ROE {attr['roe_prev']:.2%} → {attr['roe_cur']:.2%}"
            f"（{attr['change']:+.2%}）。"
        ]
        for row in attr["rows"]:
            parts.append(f"{row['因素']}贡献 {row['贡献']:+.2%}")
        citations = _citations_for(index, ["净利润", "营业收入", "资产总计", "所有者权益合计"])
        evidence = mdna_evidence(
            pdf_data, keywords=["净资产收益率", "净利润", "经营", "盈利"], limit=2)
        if evidence:
            parts.append("公司解释（原文摘录）：" + " ".join(
                f"{e['snippet']} [第{e['page']}页]" for e in evidence))
            citations += [{"page": e["page"], "snippet": e["snippet"]} for e in evidence]
        answer = " ".join(parts) + "。"
        if citations:
            answer += " 来源 " + "、".join(f"[第{c['page']}页]" for c in citations[:4]) + "。"
        return {"route": "attribution", "answer": answer, "citations": citations,
                "confidence": 0.9, "basis": "计算推断 + 原文摘录"}
    return None


# ---------------- 跨公司对比 ----------------


def _answer_compare(question: str, pdf_data: dict, peers: list, index, caliber) -> dict:
    if not peers:
        return _clarify("当前没有可对比的同行数据。请先到「横向对标」页添加至少 1 家对标公司后再问。")

    ratio = _match_ratio(question)
    account = None
    if not ratio:
        account = _match_account(_norm(question), _flat_data(pdf_data))
    if not ratio and not account:
        return _clarify("请指明要对比的指标，例如「和同行比营业收入」或「对比毛利率」。")

    label = ratio or account
    unit = ""

    def pick(pdf: dict):
        nonlocal unit
        if ratio:
            table = _ratio_table(pdf, caliber)
            row = table[table["指标"] == ratio]
            if row.empty:
                return None
            record = row.iloc[0]
            unit = record["单位"]
            val = record["数值"]
            return None if val is None or val != val else float(val)
        val = _flat_data(pdf).get(account)
        return None if val is None else float(val)

    company = (pdf_data.get("meta") or {}).get("company") or "本司"
    rows = [(p.get("company") or "同行", pick(p.get("pdf_data") or {})) for p in peers[:4]]
    rows.append((company, pick(pdf_data)))
    valid = sorted([(n, v) for n, v in rows if v is not None], key=lambda t: -t[1])
    if not valid:
        return _clarify(f"未能从各家报表中取到「{label}」的可用数值。")

    def fmt(val):
        return _fmt_value(val, unit) if ratio else f"{val / 1e8:.2f} 亿元"

    answer = f"{label}对比（本期）：" + "｜".join(f"{n} {fmt(v)}" for n, v in valid)
    rank = next((i for i, (n, _) in enumerate(valid, 1) if n == company), None)
    if rank:
        answer += f"；本司排名 {rank}/{len(valid)}"
    missing = [n for n, v in rows if v is None]
    if missing:
        answer += f"；{'、'.join(missing)} 数据缺失"
    answer += "。"

    citations = []
    if index is not None:
        if account:
            citations = _citations_for(index, [account])
        else:
            for item in indicator_evidence(index, label, use_llm=False):
                if item.get("found"):
                    for page in item.get("pages", [])[:1]:
                        citations.append({"page": page,
                                          "snippet": f"{item['科目']}：{item['text']}"})
    for name, val in rows:
        if val is None or name == company:
            continue
        peer = next((p for p in peers if (p.get("company") or "同行") == name), None)
        if not peer:
            continue
        try:
            peer_index = build_index(peer["pdf_data"])
            entries = (peer_index.find(account) if account
                       else [])
            if entries:
                citations.append({"page": entries[0]["page"],
                                  "snippet": f"{name}：{entries[0]['text']}",
                                  "company": name})
        except Exception:            # 同行文档异常不影响本司结论
            continue
    return {"route": "compare", "answer": answer, "citations": citations[:6],
            "confidence": 0.9, "basis": "多份报表确定性计算"}


# ---------------- 兜底与拦截 ----------------


def _clarify(message: str) -> dict:
    return {"route": "clarify", "answer": message, "citations": [],
            "confidence": 0.5, "basis": "需要用户补充信息"}


def _refuse(question: str, reason: str = "") -> dict:
    hints = []
    if any(w in question for w in ("风险", "诉讼", "担保", "质押", "不确定")):
        hints.append("风险因素 / 或有事项 / 股权质押")
    if any(w in question for w in ("分红", "股利", "股息")):
        hints.append("利润分配 / 股利政策")
    if any(w in question for w in ("关联", "交易")):
        hints.append("关联方及关联交易")
    if any(w in question for w in ("收入", "毛利", "利润", "成本")):
        hints.append("管理层讨论与分析 / 合并利润表")
    if any(w in question for w in ("现金", "回款")):
        hints.append("合并现金流量表 / 经营活动分析")
    locations = "、".join(dict.fromkeys(hints)) or "管理层讨论与分析 / 财务报表附注"
    prefix = f"{reason}。" if reason else ""
    return {"route": "refuse",
            "answer": f"{prefix}资料中未找到可引用依据。建议查阅：{locations}。",
            "citations": [], "confidence": 0.0, "basis": "无可用依据"}


def _refuse_offtopic() -> dict:
    return {"route": "refuse",
            "answer": ("该问题与财报分析无关。本助手仅回答年报/财报相关问题，"
                       "例如：营业收入、ROE、毛利率变化原因。"),
            "citations": [], "confidence": 0.0, "basis": "问题与财报无关"}


# ---------------- 多轮指代 ----------------


def _followup_entity(question: str, history, data: dict):
    """省略句继承：如“那去年呢”沿用上一轮指标。仅含指代词时触发。"""
    if not history or not any(w in question for w in _FOLLOWUP_HINTS):
        return None
    for msg in reversed(history):
        if (msg.get("role") or "") != "user":
            continue
        prev = _norm(msg.get("content") or "")
        if not prev:
            continue
        ratios = _match_ratio_all(prev)
        if len(ratios) == 1:
            return "ratio", ratios[0]
        account = _match_account(prev, data)
        if account:
            return "account", account
    return None


# ---------------- 主入口 ----------------


def answer(question: str, pdf_data: dict, index=None, use_llm: bool = True,
           caliber="标准", history=None, peers=None) -> dict:
    """问答主入口：数字题确定性计算，文本题检索 + 可选 LLM，无引用则拒答。"""
    question = (question or "").strip()
    if not question:
        return _refuse("")
    if len(question) > 300:
        question = question[:300]
    q = _norm(question)
    index = index or build_index(pdf_data)

    if not (pdf_data.get("pages") or []):
        return _refuse(question, "未解析到可检索内容，请换用文字版年报后重试")

    if any(h in q for h in _COMPARE_HINTS):
        if peers:
            return _answer_compare(question, pdf_data, peers, index, caliber)
        return _clarify("当前没有可对比的同行数据。请先到「横向对标」页添加至少 1 家对标公司后再问。")

    period = _period_intent(q)

    if _is_numeric(q):
        ratios = _match_ratio_all(q)
        if len(ratios) > 1:
            return _clarify("这次问题包含多个指标：" + "、".join(ratios) + "。请指明其中一个。")
        if ratios:
            return _answer_ratio(ratios[0], pdf_data, index, question, caliber, period)
        account = _match_account(q, _flat_data(pdf_data))
        if account:
            return _answer_account(account, question, _flat_data(pdf_data), index, period)

    follow = _followup_entity(q, history, _flat_data(pdf_data))
    if follow:
        kind, name = follow
        if kind == "ratio":
            return _answer_ratio(name, pdf_data, index, question, caliber, period)
        return _answer_account(name, question, _flat_data(pdf_data), index, period)

    if any(h in q for h in ("为什么", "原因", "为何", "说明", "解释", "分析")):
        special = _answer_attribution(question, pdf_data, index)
        if special:
            return special

    citations, top = _retrieve_scored(question, pdf_data)
    related = _is_finance_related(q)
    if not citations:
        return _refuse(question) if related else _refuse_offtopic()
    if not related and top < 3:
        return _refuse_offtopic()

    weight = 0.55 + 0.35 * min(top / 12.0, 1.0)
    if use_llm:
        generated = _llm_answer(question, citations)
        if generated:
            return {"route": "text", "answer": generated, "citations": citations,
                    "confidence": round(min(0.85, weight), 2),
                    "basis": "LLM 生成，已通过引用与数字回查"}
    return {"route": "text", "answer": _extractive(citations), "citations": citations,
            "confidence": round(min(0.70, weight), 2), "basis": "原文摘录"}
