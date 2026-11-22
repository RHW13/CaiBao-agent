"""财报语义问答：数字题走确定性计算，文本题走检索 + 可选 LLM，强制引用。"""
import re

from . import config
from .attribution import dupont_attribution, gross_margin_breakdown, mdna_evidence
from .evidence import indicator_evidence
from .notes_scan import build_notes
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

_SYS_PROMPT = (
    "你是财报问答助手。只能依据下面提供的原文片段回答，不得使用片段外的知识。\n"
    "每个结论后必须标注来源页码，格式为 [第X页]。\n"
    "片段不足以回答时，直接回答“资料中未找到依据”，并说明可能查阅的章节。\n"
    "涉及数字时只引用片段中已有的数字，不要自行加减乘除。"
)


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


def _match_ratio(q: str):
    lower = q.lower()
    for alias in sorted(_RATIO_ALIASES, key=len, reverse=True):
        if alias.lower() in lower:
            return _RATIO_ALIASES[alias]
    return None


def _account_terms(data: dict):
    terms = list(_ACCOUNT_ALIASES.items())
    terms += [(k, k) for k in data if not k.endswith(("_上期", "_平均"))]
    return sorted(terms, key=lambda item: -len(item[0]))


def _match_account(q: str, data: dict):
    for term, canonical in _account_terms(data):
        if term in q:
            return canonical
    return None


def _answer_ratio(name: str, pdf_data: dict, index, question: str, caliber="标准") -> dict:
    ratios = compute_ratios(pdf_data, caliber=caliber)
    row = ratios[ratios["指标"] == name]
    if row.empty:
        return {"route": "ratio", "answer": f"未找到指标「{name}」。", "citations": []}
    record = row.iloc[0]
    value = record["数值"]
    if value is None or value != value:
        reason = record["备注"] or "数据缺失"
        return {"route": "ratio", "answer": f"{name} 当前无法计算：{reason}。", "citations": []}

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
    if citations:
        answer += "关键科目见 " + "、".join(f"[第{c['page']}页]" for c in citations[:3]) + "。"
    return {"route": "ratio", "answer": answer, "citations": citations,
            "value": value, "unit": record["单位"]}


def _answer_account(name: str, question: str, pdf_data: dict, index) -> dict:
    data = build_data(pdf_data)
    current = data.get(name)
    if current is None:
        return {"route": "account", "answer": f"{name}：资料中未找到该科目。", "citations": []}
    previous = data.get(name + "_上期")
    growth = (current - previous) / abs(previous) if previous else None

    parts = [f"{name}：{current:,.2f} 元（约 {current / 1e8:.2f} 亿元）"]
    if previous is not None:
        parts.append(f"上期 {previous:,.2f} 元")
    if growth is not None:
        parts.append(f"同比 {growth:+.2%}")
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
    return {"route": "account", "answer": answer, "citations": citations, "value": current}


def _keywords(question: str) -> list:
    text = question
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
        if hint in question:
            tokens.extend(phrases)
    for alias, canonical in _RATIO_ALIASES.items():
        if alias.lower() in question.lower():
            tokens.extend([alias, canonical])
    for alias, canonical in _ACCOUNT_ALIASES.items():
        if alias in question:
            tokens.extend([alias, canonical])

    cleaned = [t for t in tokens if len(t) >= 2 and t not in _STOPWORDS]
    return sorted(dict.fromkeys(cleaned), key=len, reverse=True)[:12]


def _retrieve(question: str, pdf_data: dict, limit: int = 4) -> list:
    notes = build_notes(pdf_data.get("pages") or [])
    keywords = _keywords(question)
    starts = [p for p in (pdf_data.get("stmt_starts") or {}).values() if p]
    upper = min(starts) if starts else 10 ** 9

    if any(w in question for w in ("风险", "诉讼", "担保", "质押", "不确定")):
        sections = ["风险因素", "管理层讨论与分析"]
    elif any(w in question for w in ("经营", "业务", "原因", "为什么", "发展", "战略")):
        sections = ["管理层讨论与分析"]
    else:
        sections = None

    hits = notes.search(keywords, sections=sections, limit=limit * 3, pad=220)
    hits = [h for h in hits if 5 < h["page"] < upper]
    if not hits and sections:
        hits = [h for h in notes.search(keywords, limit=limit * 3, pad=220)
                if 5 < h["page"] < upper]
    if not hits:
        hits = notes.search(keywords, limit=limit, pad=220)
    return [{"page": h["page"], "snippet": h["snippet"]} for h in hits[:limit]]


def _num_tokens(text: str) -> set:
    """抽取文本中的数字（去掉千分位与百分号），用于可核验性检查。"""
    tokens = set()
    for t in re.findall(r"\d[\d,]*(?:\.\d+)?", text):
        tokens.add(t.replace(",", ""))
    return tokens


def _llm_answer(question: str, citations: list) -> str:
    client = config.get_llm_client()
    if client is None:
        return ""
    block = "\n".join(f"[第{c['page']}页] {c['snippet']}" for c in citations)
    try:
        resp = client.chat.completions.create(
            model=config.get_llm_model(),
            messages=[
                {"role": "system", "content": _SYS_PROMPT},
                {"role": "user", "content": f"原文片段：\n{block}\n\n问题：{question}"},
            ],
            temperature=0,
        )
        answer = (resp.choices[0].message.content or "").strip()
    except Exception:
        return ""
    valid = {c["page"] for c in citations}
    cited = {int(p) for p in re.findall(r"第(\d+)页", answer)}
    if not (cited & valid):
        return ""
    # 数字回查：答案中出现的每个数字都必须能在引用原文中找到，否则视为不可核验
    # （模型自行推算/编造），退回原文摘录，避免编造数字误导用户。
    body = re.sub(r"第\s*\d+\s*页", " ", answer)
    allowed = _num_tokens(" ".join(c["snippet"] for c in citations))
    if _num_tokens(body) - allowed:
        return ""
    return answer


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


def _answer_attribution(question: str, pdf_data: dict, index):
    """原因类问题：先给系统计算推断，再附公司解释原文。"""
    if "毛利率" in question:
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
        return {"route": "attribution", "answer": answer, "citations": citations}

    if "roe" in question.lower() or "净资产收益率" in question:
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
        return {"route": "attribution", "answer": answer, "citations": citations}
    return None


def _refuse(question: str) -> dict:
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
    return {"route": "refuse",
            "answer": f"资料中未找到可引用依据。建议查阅：{locations}。",
            "citations": []}


def answer(question: str, pdf_data: dict, index=None, use_llm: bool = True, caliber="标准") -> dict:
    """问答主入口：数字题确定性计算，文本题检索 + 可选 LLM，无引用则拒答。"""
    question = (question or "").strip()
    if not question:
        return _refuse(question)
    index = index or build_index(pdf_data)

    if _is_numeric(question):
        ratio = _match_ratio(question)
        if ratio:
            return _answer_ratio(ratio, pdf_data, index, question, caliber=caliber)
        account = _match_account(question, build_data(pdf_data))
        if account:
            return _answer_account(account, question, pdf_data, index)

    if any(h in question for h in ("为什么", "原因", "为何", "说明", "解释", "分析")):
        special = _answer_attribution(question, pdf_data, index)
        if special:
            return special

    citations = _retrieve(question, pdf_data)
    if not citations:
        return _refuse(question)
    if use_llm:
        generated = _llm_answer(question, citations)
        if generated:
            return {"route": "text", "answer": generated, "citations": citations}
    return {"route": "text", "answer": _extractive(citations), "citations": citations}
