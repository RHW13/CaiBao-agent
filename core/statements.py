"""三表结构化：将 PDF 词条重建为 资产负债表 / 利润表 / 现金流量表 DataFrame。"""
import re

import pandas as pd

from . import pdf_loader
from .mapping import clean_account, normalize_account, get_aliases

# 各报表的结束锚点（规范科目名）；解析到即停止，避免把母公司报表并入
END_MARKERS = {
    "balance_sheet": ["负债和所有者权益总计", "负债及所有者权益总计", "负债和股东权益总计",
                      "负债及股东权益总计", "母公司资产负债表",
                      "股东权益变动表", "所有者权益变动表"],
    "income": ["稀释每股收益", "母公司利润表", "股东权益变动表", "所有者权益变动表"],
    "cashflow": ["期末现金及现金等价物余额", "年末现金及现金等价物余额", "母公司现金流量表",
                 "股东权益变动表", "所有者权益变动表"],
}

# 数值词：可带负号/千分位/小数，也支持会计常用的括号负数（如 (1,234)）；
# 必须含至少一位数字（避免单独的 "-" 或 ","）
_NUM_RE = re.compile(r"^[（(]?[-－]?(?=[\d,]*\d)[\d,]+(?:\.\d+)?[）)]?$")

_KNOWN_ACCOUNTS = None


def _known_accounts() -> set:
    global _KNOWN_ACCOUNTS
    if _KNOWN_ACCOUNTS is None:
        aliases = get_aliases()
        _KNOWN_ACCOUNTS = set(aliases.keys()) | set(aliases.values())
    return _KNOWN_ACCOUNTS


def _is_known(text: str) -> bool:
    return clean_account(text) in _known_accounts()


_BAD_NAME = re.compile(r"[，。；：]")


def _valid_name(name: str) -> bool:
    """过滤正文段落被误拼成「科目名」的垃圾行（过长或含句读）。"""
    if not name or len(name) > 36:
        return False
    if _BAD_NAME.search(name):
        return False
    return bool(re.search(r"[\u4e00-\u9fa5A-Za-z]", name))


def _parse_num(s: str) -> float:
    """解析数值词；括号包裹视为负数，如 (1,234) -> -1234。"""
    text = s.replace(",", "").replace("－", "-").strip()
    negative = text.startswith(("(", "（")) and text.endswith((")", "）"))
    text = text.strip("()（）").strip()
    if not text or text == "-":
        return 0.0
    value = float(text)
    return -abs(value) if negative else value


def _value_columns(rows: list, tol: float = 18.0):
    """识别页面上的「数值列」x 中心。

    银行等报表在科目名后有一列纯数字的附注编号（如 1、2、3…），位列最左且数值很小；
    该列会被剔除，避免被误当作本期/上期金额。返回前两个数值列中心（合并口径）。
    """
    points = []
    for row in rows:
        for w in row:
            if _NUM_RE.match(w["text"]):
                points.append(((w["x0"] + w["x1"]) / 2, _parse_num(w["text"])))
    if not points:
        return []
    points.sort()
    clusters = []
    for x, v in points:
        if clusters and x - clusters[-1][-1][0] <= tol:
            clusters[-1].append((x, v))
        else:
            clusters.append([(x, v)])

    def is_note_col(cluster) -> bool:
        vals = [abs(v) for _, v in cluster]
        small_int = sum(1 for v in vals if v < 1000 and float(v).is_integer())
        return small_int / len(vals) >= 0.8 and sum(vals) / len(vals) < 200

    centers = [sum(x for x, _ in c) / len(c) for c in clusters]
    # 左侧可能依次出现页码、附注编号等小整数列，逐列剔除
    while clusters and is_note_col(clusters[0]):
        clusters, centers = clusters[1:], centers[1:]
    return centers[:2]


def _cluster_rows(words: list, tol: float = 3.0) -> list:
    """按 top 坐标把词条聚类成行（返回按 y 排序的行列表）。"""
    ordered = sorted(words, key=lambda w: (w["top"], w["x0"]))
    rows = []
    cur = []
    anchor = None
    for w in ordered:
        if anchor is None or abs(w["top"] - anchor) <= tol:
            cur.append(w)
            if anchor is None:
                anchor = w["top"]
        else:
            rows.append(cur)
            cur = [w]
            anchor = w["top"]
    if cur:
        rows.append(cur)
    return rows


# 供 pdf_index 复用
cluster_rows = _cluster_rows


def _row_bbox(row: list, page: int) -> dict:
    """计算一行词条的包围盒与原文。"""
    return {
        "page": page,
        "x0": min(w["x0"] for w in row),
        "x1": max(w["x1"] for w in row),
        "top": min(w["top"] for w in row),
        "bottom": max(w["bottom"] for w in row),
    }


def _merge_bbox(meta: dict, row_meta: dict, extra_text: str) -> None:
    """把跨行/跨页的坐标与文本合并进已有证据。"""
    meta["x0"] = min(meta["x0"], row_meta["x0"])
    meta["x1"] = max(meta["x1"], row_meta["x1"])
    meta["top"] = min(meta["top"], row_meta["top"])
    meta["bottom"] = max(meta["bottom"], row_meta["bottom"])
    if row_meta["page"] not in meta["pages"]:
        meta["pages"].append(row_meta["page"])
    meta["text"] += extra_text


_DATE_ROW_RE = re.compile(r"^\d{0,4}年?\d{0,2}月\d{1,2}日?$")
# 报告期：年度 / 半年度 / 第一季度 / 第三季度 / 中期（兼容「2025年度报告」写法）
_PERIOD_RE = re.compile(r"(20\d{2})\s*年\s*(半年度|第[一三]季度|年度|度|中期)?\s*报告")
# 公司名：优先股份有限公司，其次有限责任公司/有限公司
_COMPANY_RE = re.compile(
    r"([\u4e00-\u9fa5A-Za-z（）()·]+(?:股份有限公司|有限责任公司|有限公司))"
)
_PAGE_NO_RE = re.compile(r"^\d+/\d+$")
_NOTE_ONLY_RE = re.compile(r"^(?:[一二三四五六七八九十]+、)?\d+$")
_NOISE_KWS = ("公司负责人", "会计机构负责人", "主管会计工作负责人",
              "单位：", "币种", "编制单位")
# 报表表头提示词（首次出现即视为本表开始）
_HEADER_HINTS = ("项目", "期末余额", "年末余额", "年初余额",
                 "本期金额", "本期发生额", "本年金额")


def _is_noise_row(text: str) -> bool:
    """页眉、页码、日期表头、单位行、孤立附注编号等非科目行。"""
    if not text:
        return False
    if _PAGE_NO_RE.match(text) or _DATE_ROW_RE.match(text) or _NOTE_ONLY_RE.match(text):
        return True
    if "年年度报告" in text:
        return True
    if any(k in text for k in _NOISE_KWS):
        return True
    return "年度" in text and len(text) <= 20 and not _is_known(text)


def _looks_like_suffix(fragment: str) -> bool:
    """换行续写片段：附注引用、以“的/额/计”等收尾词开头，或为 1-2 字的闭合片段。"""
    if not fragment:
        return False
    if re.fullmatch(r"[（(][^（）()]{0,15}[）)]", fragment):
        return True  # 纯附注引用，如「（附注四、44）」
    if fragment[0] in "）)":
        return True
    if fragment.startswith("的") or fragment.startswith("权益"):
        return True
    if fragment in ("额", "计", "股)", "股）"):
        return True
    return fragment.startswith("净") and len(fragment) <= 4


def _merge_name(name: str, fragment: str):
    """判断 fragment 是否科目名续行；是则返回合并名，否则 None。"""
    merged = name + fragment
    if _is_known(merged):
        return merged
    if _looks_like_suffix(fragment):
        return merged
    return None


_TITLE_PREFIX = r"^(?:合并及母公司|合并及公司|合并|母公司|公司)?"
# 遇到其它报表的页标题即停止（防止报表边界识别失败时串读）
_STOP_TITLES = {
    "balance_sheet": re.compile(_TITLE_PREFIX +
                                r"(?:利润表|损益表|现金流量表|股东权益变动表|所有者权益变动表)"),
    "income": re.compile(_TITLE_PREFIX +
                         r"(?:现金流量表|股东权益变动表|所有者权益变动表)"),
    "cashflow": re.compile(_TITLE_PREFIX + r"(?:股东权益变动表|所有者权益变动表)"),
}


def _parse_statement(pages: list, start_page: int, end_markers: list, stop_page: int = None,
                     stop_titles=None):
    """从起始页起重建单张报表，返回 (rows_data, unknown_accounts)。"""
    end_set = set(end_markers)
    rows_data = []
    unknown = set()
    cur = None  # {"name": str, "vals": [本期, 上期] | None, "meta": dict}
    stopped = False
    # 起始页由上一步的报表标题定位，故进入起始页即开始解析；
    # 「项目/期末余额」等表头行仅用于丢弃表头本身。
    started = False

    def new_meta(row_meta: dict, text: str) -> dict:
        meta = dict(row_meta)
        meta["pages"] = [row_meta["page"]]
        meta["text"] = text
        return meta

    def flush():
        nonlocal cur
        if cur is not None:
            norm = normalize_account(cur["name"])
            if cur["vals"] is not None and _valid_name(norm):
                rows_data.append(
                    {
                        "name": norm,
                        "本期": cur["vals"][0],
                        "上期": cur["vals"][1] if len(cur["vals"]) > 1 else None,
                        "trace": cur["meta"],
                    }
                )
                if not _is_known(norm):
                    unknown.add(norm)
            cur = None

    page_rows = []
    for pg in pages:
        if pg["page"] < start_page:
            continue
        if stop_page is not None and pg["page"] >= stop_page:
            break
        rows = _cluster_rows(pg["words"])
        page_rows.append((pg, rows, _value_columns(rows)))

    for pg, rows, value_cols in page_rows:
        if pg["page"] == start_page:
            started = True  # 起始页由报表标题定位，直接开始
        for row in rows:
            text_words = [w for w in row if not _NUM_RE.match(w["text"])]
            num_words = [w for w in row if _NUM_RE.match(w["text"])]
            if value_cols:  # 只保留金额列，剔除附注编号列
                num_words = [
                    w for w in num_words
                    if any(abs((w["x0"] + w["x1"]) / 2 - c) <= 20 for c in value_cols)
                ]
            text = "".join(w["text"] for w in sorted(text_words, key=lambda w: w["x0"]))
            nums = [_parse_num(w["text"]) for w in sorted(num_words, key=lambda w: w["x0"])]
            row_meta = _row_bbox(row, pg["page"])

            # 表头行（含“项目/期末余额/本期金额”等）：首次遇到视为本表开始
            if any(h in text for h in _HEADER_HINTS):
                started = True
                flush()
                continue
            if not started:
                continue
            if stop_titles is not None and pg["page"] != start_page and stop_titles.match(text):
                flush()
                stopped = True
                break
            if _is_noise_row(text):
                continue

            if not text and nums:
                # 纯数值行：名称与数值分行时，数值归入当前科目
                if cur is not None and cur["vals"] is None:
                    cur["vals"] = nums
                    _merge_bbox(cur["meta"], row_meta, "")
                continue

            if text and nums:
                if cur is not None and cur["vals"] is None:
                    merged = _merge_name(cur["name"], text)
                    if merged is not None:
                        cur["name"] = merged
                        cur["vals"] = nums
                        _merge_bbox(cur["meta"], row_meta, text)
                    else:
                        flush()
                        cur = {"name": text, "vals": nums, "meta": new_meta(row_meta, text)}
                else:
                    flush()
                    cur = {"name": text, "vals": nums, "meta": new_meta(row_meta, text)}
            elif text and not nums:
                if text.endswith("：") or text.endswith(":"):
                    flush()  # 节标题
                elif cur is not None:
                    merged = _merge_name(cur["name"], text)
                    if merged is not None:
                        cur["name"] = merged
                        _merge_bbox(cur["meta"], row_meta, text)
                        if cur["vals"] is not None:
                            flush()  # 数值已在前一行，补全名称后即完成
                    else:
                        flush()
                        cur = {"name": text, "vals": None, "meta": new_meta(row_meta, text)}
                else:
                    cur = {"name": text, "vals": None, "meta": new_meta(row_meta, text)}
            # 无数值无文本：孤立数字（页脚页码等）忽略

            if text and any(marker in normalize_account(text) for marker in end_set):
                flush()
                stopped = True
                break
        if stopped:
            break

    flush()
    return rows_data, unknown


def _to_dataframe(rows_data: list, stmt: str) -> pd.DataFrame:
    if stmt == "balance_sheet":
        cols = ["科目", "本期期末", "上期期末"]
    else:
        cols = ["科目", "本期金额", "上期金额"]
    records = [{"科目": r["name"], cols[1]: r["本期"], cols[2]: r["上期"]} for r in rows_data]
    return pd.DataFrame(records, columns=cols)


def _extract_meta(pages: list) -> dict:
    """提取公司名与报告期（年度报告 / 半年度报告 / 第X季度报告）。"""
    first = pages[0]["text"] if pages else ""
    p = _PERIOD_RE.search(first)
    year = p.group(1) if p else ""
    period = ""
    if p:
        raw = p.group(2) or "年度"
        if raw == "中期":
            period = "半年度报告"
        elif raw in ("年度", "度"):
            period = "年度报告"
        else:  # 半年度 / 第一季度 / 第三季度
            period = raw + "报告"
    c = _COMPANY_RE.search(first)
    company = c.group(1) if c else ""
    return {"company": company, "year": year, "period": period or "年度报告"}


def load_pdf(path: str) -> dict:
    """解析年报 PDF，返回三表 DataFrame 与元信息。

    额外返回 traces：{报表键: [{account, page, pages, x0, x1, top, bottom, text, values}]}，
    记录每个科目在 PDF 中的页码与坐标，供指标溯源使用。
    """
    pages = pdf_loader.extract_pages(path)
    starts = pdf_loader.detect_statement_pages(pages)

    result = {
        "balance_sheet": None,
        "income": None,
        "cashflow": None,
        "pages": pages,
        "meta": _extract_meta(pages),
        "unknown_accounts": [],
        "warnings": [],
        "traces": {"balance_sheet": [], "income": [], "cashflow": []},
        "stmt_starts": starts,
    }

    labels = {"balance_sheet": "资产负债表", "income": "利润表", "cashflow": "现金流量表"}
    starts_order = sorted(p for p in starts.values() if p)

    def stop_for(start: int) -> int:
        later = [p for p in starts_order if p > start]
        return min(later) if later else start + 12

    for stmt, start in starts.items():
        if start is None:
            result["warnings"].append(
                f"未定位到「{labels[stmt]}」：未找到含「项目」表头且数值密集的报表页。"
                "请确认 PDF 为电子版（有文字层）且包含完整财务报表。"
            )
            continue
        rows_data, unknown = _parse_statement(
            pages, start, END_MARKERS[stmt], stop_page=stop_for(start),
            stop_titles=_STOP_TITLES[stmt],
        )
        result[stmt] = _to_dataframe(rows_data, stmt)
        result["unknown_accounts"].extend(unknown)
        for r in rows_data:
            trace = dict(r["trace"])
            trace["account"] = r["name"]
            trace["stmt"] = stmt
            trace["values"] = [r["本期"], r["上期"]]
            result["traces"][stmt].append(trace)

    return result
