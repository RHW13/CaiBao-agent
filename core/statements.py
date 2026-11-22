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

# 4 位年份（如 2025/2024）：报表表头列标签，非金额值，数值列识别时应排除，
# 否则会把「2025年12月31日」「2024年度」等表头词误当作金额列，导致本期/上期列丢失。
_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")

# 季报利润表/现金流量表的累计期表头：出现这些词说明页面为四列布局
# （本报告期 | 上年同期 | 年初至报告期末 | 上年同期累计），应取累计期两列。
_YTD_HINTS = ("年初至报告期末", "年初至本报告期末", "年初至报告期期末",
              "本年累计", "年初至本期")

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


def _header_mode(rows: list):
    """识别数值列布局：'ytd'（季报累计期）或 'entity'（合并/公司并列）。

    合并及公司报表常在同一张表内并列四列（合并本期/上期、公司本期/上期），
    应在表头行出现独立的「合并」「公司」词时判定为 entity；季报四列的时间口径
    则出现「年初至报告期末」等累计期表头，判定为 ytd。
    """
    for row in rows[:12]:
        words = [w["text"] for w in row]
        if words.count("合并") >= 2 and "公司" in words:
            return "entity"
    text = "".join(w["text"] for row in rows[:10] for w in row)
    if any(k in text for k in _YTD_HINTS):
        return "ytd"
    return None


def _value_columns(rows: list, tol: float = 18.0, mode: str = None):
    """识别页面上的「数值列」x 中心。

    银行等报表在科目名后有一列纯数字的附注编号（如 1、2、3…），位列最左且数值很小；
    该列会被剔除，避免被误当作本期/上期金额。页脚页码（孤立的小整数）同样剔除，
    否则会占掉最右列，把真正的上期列挤掉。返回 [本期列, 上期列]。
    """
    tops = [w["top"] for row in rows for w in row]
    y_max = max(tops) if tops else 0.0
    y_min = min(tops) if tops else 0.0
    footer_zone = y_max - max(20.0, (y_max - y_min) * 0.06)
    points = []
    for row in rows:
        row_nums = [w for w in row if _NUM_RE.match(w["text"]) and not _YEAR_RE.match(w["text"])]
        for w in row_nums:
            value = _parse_num(w["text"])
            # 页脚页码：位于页面底部、该行仅此一个数字、且为小整数
            if (len(row_nums) == 1 and w["top"] >= footer_zone
                    and abs(value) < 10000 and float(value).is_integer()):
                continue
            points.append(((w["x0"] + w["x1"]) / 2, w["x1"], value))
    if not points:
        return []
    points.sort()
    clusters = []
    for x, x1, v in points:
        if clusters and x - clusters[-1][-1][0] <= tol:
            clusters[-1].append((x, x1, v))
        else:
            clusters.append([(x, x1, v)])

    def is_note_col(cluster) -> bool:
        vals = [abs(v) for _, _, v in cluster]
        small_int = sum(1 for v in vals if v < 1000 and float(v).is_integer())
        return small_int / len(vals) >= 0.8 and sum(vals) / len(vals) < 200

    # 金额列多为右对齐，同时记录「中心」与「右边界」：短数字（如每股收益 0.35）
    # 中心会明显偏左，只按中心判定会被误判成其它列而丢失。
    cols = [
        {"center": sum(x for x, _, _ in c) / len(c),
         "right": sum(x1 for _, x1, _ in c) / len(c)}
        for c in clusters
    ]
    # 左侧可能依次出现页码、附注编号等小整数列，逐列剔除
    while clusters and is_note_col(clusters[0]):
        clusters, cols = clusters[1:], cols[1:]
    if len(cols) >= 2:
        mode = mode or _header_mode(rows)
        # 四列布局有两种口径，须按表头选择正确的一对金额列：
        #   ytd    —— 本报告期 | 上年同期 | 年初至报告期末 | 上年同期累计
        #   entity —— 合并本期 | 合并上期 | 公司本期 | 公司上期
        if mode == "ytd" and len(cols) >= 4:
            return [cols[-2], cols[-1]]
        if mode == "entity" and len(cols) >= 3:
            return [cols[0], cols[1]]
        # 取最左（本期）与最右（上期）两列：中间可能出现的孤立散点列（个别合计行
        # 数值因 PDF 排版错位形成的错位列）不参与，避免真正的上期列被挤掉。
        return [cols[0], cols[-1]]
    return cols


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
# 封面用中文数字年份（如「二〇二六年半年度报告」）时的兜底
_CN_YEAR_RE = re.compile(r"([二〇零一二三四五六七八九]{4})年")
_CN_DIGIT = {"〇": "0", "零": "0", "一": "1", "二": "2", "三": "3", "四": "4",
             "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"}
# 报告期关键词兜底：封面正文未写「20XX年…报告」时按关键词判断，
# 半年度/季度需先于「年度报告」匹配（前者包含后者子串）。
_PERIOD_KWS = (("半年度报告", "半年度报告"), ("中期报告", "半年度报告"),
               ("第三季度报告", "第三季度报告"), ("三季度报告", "第三季度报告"),
               ("第一季度报告", "第一季度报告"), ("年度报告", "年度报告"))
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


def _find_header_index(rows: list) -> int:
    """定位财务报表表头行（首个以「项目」为最左列标题的行），返回其索引；找不到返回 -1。

    仅以「最左词以『项目』开头」判定，避免把审计页「（项目合伙人）」这类含「项目」的正文行误判为表头。
    """
    for i, row in enumerate(rows):
        words = sorted(row, key=lambda w: w["x0"])
        if words and words[0]["text"].startswith("项目"):
            return i
    return -1


# 母公司三表标题：其后的行属于母公司报表，若与合并报表同页，会污染数值列识别，
# 也会被误并入合并报表，故以此作为合并报表的解析边界。
_PARENT_TITLES = ("母公司资产负债表", "母公司利润表", "母公司现金流量表")


def _find_parent_index(rows: list) -> int:
    """定位首个母公司报表标题行的索引；找不到返回 -1。"""
    for i, row in enumerate(rows):
        text = "".join(w["text"] for w in sorted(row, key=lambda w: w["x0"]))
        if any(t in text for t in _PARENT_TITLES):
            return i
    return -1


def _find_gutter(rows: list):
    """识别左右双栏排版（左栏资产、右栏负债），返回栏间分割线 x；非双栏返回 None。

    判定依据：表头行出现两个「项目」列标题（每栏各一个）。分栏线取「左栏最右金额的
    右边界」与「右栏科目名最左边界」的中点——右栏科目名常比右栏表头更靠左，
    只按表头中点分割会把右栏科目名误切到左栏。
    """
    two_heads = False
    for row in rows:
        words = sorted(row, key=lambda w: w["x0"])
        if sum(1 for w in words if w["text"].startswith("项目")) >= 2:
            two_heads = True
            break
    if not two_heads:
        return None

    nums = []
    for row in rows:
        for w in row:
            if _NUM_RE.match(w["text"]) and not _YEAR_RE.match(w["text"]):
                cx = (w["x0"] + w["x1"]) / 2
                nums.append((w["x0"], w["x1"], cx, _parse_num(w["text"])))
    if len(nums) < 6:
        return None
    clusters = []
    for item in sorted(nums, key=lambda t: t[2]):
        if clusters and item[2] - clusters[-1][-1][2] <= 18:
            clusters[-1].append(item)
        else:
            clusters.append([item])
    # 剔除孤立的小整数散点（个别附注编号被单独排成数值词），避免把栏目切歪
    clusters = [
        cl for cl in clusters
        if not (len(cl) < 3 and all(abs(v) < 1000 and float(v).is_integer()
                                    for _, _, _, v in cl))
    ]
    if len(clusters) < 3:
        return None
    centers = [sum(item[2] for item in cl) / len(cl) for cl in clusters]
    gaps = [(centers[i + 1] - centers[i], i) for i in range(len(centers) - 1)]
    _, i = max(gaps)
    left_x1 = max(x1 for _, x1, _, _ in clusters[i])
    rights = [w["x0"] for row in rows for w in row
              if not _NUM_RE.match(w["text"]) and w["x0"] > left_x1]
    if not rights:
        return None
    return (left_x1 + min(rights)) / 2.0


def _split_blocks(rows: list, gutter: float):
    """按分栏线把行切成左右两组（各自保留原有 y 顺序）。"""
    left, right = [], []
    for row in rows:
        lw = [w for w in row if (w["x0"] + w["x1"]) / 2 < gutter]
        rw = [w for w in row if (w["x0"] + w["x1"]) / 2 >= gutter]
        if lw:
            left.append(lw)
        if rw:
            right.append(rw)
    return left, right


def _both_sides_have_nums(rows: list, gutter: float, least: int = 3) -> bool:
    """确认某页确实是双栏：分栏线两侧都有足够多的数值词。"""
    l = r = 0
    for row in rows:
        for w in row:
            if not _NUM_RE.match(w["text"]):
                continue
            if (w["x0"] + w["x1"]) / 2 < gutter:
                l += 1
            else:
                r += 1
    return l >= least and r >= least


def _footer_zone(rows: list) -> float:
    """页面底部的页码区起点 y（低于该 y 且孤立的小整数视为页脚页码）。"""
    tops = [w["top"] for row in rows for w in row]
    if not tops:
        return 0.0
    y_max, y_min = max(tops), min(tops)
    return y_max - max(20.0, (y_max - y_min) * 0.06)


def _is_footer_row(row: list, zone: float) -> bool:
    """页脚页码行：位于页面底部、只有一个词且为小整数。

    此类行若不剔除，会被当作「孤行金额」并入上一科目，把页码写成金额。
    """
    if len(row) != 1 or not _NUM_RE.match(row[0]["text"]):
        return False
    if row[0]["top"] < zone:
        return False
    value = _parse_num(row[0]["text"])
    return abs(value) < 10000 and float(value).is_integer()


def _parse_statement(pages: list, start_page: int, end_markers: list, stop_page: int = None,
                     stop_titles=None):
    """从起始页起重建单张报表，返回 (rows_data, unknown_accounts, blank_accounts)。

    blank_accounts：科目名存在、但本期与上期金额均为空白的规范科目（空白即无余额，
    由上层按 0 计），不写入 rows_data，避免污染报表原样展示。
    """
    end_set = set(end_markers)
    rows_data = []
    unknown = set()
    blanks = set()
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
            elif cur["vals"] is None and _valid_name(norm) and _is_known(norm):
                # 规范科目但两期金额均空白：记为「空白科目」，供比率层按 0 处理。
                # 仅收录词典内的标准科目，避免把换行碎片误当作 0。
                blanks.add(norm)
            cur = None

    # 孤行金额：某些报表把「本期金额」单独打印成一行（科目名与上期金额在下一行），
    # 暂存后并入紧接着出现的科目行，避免该期数值丢失。
    pending = [None]

    def absorb(vals):
        """把暂存的孤行金额补进当前行的空缺期次；返回合并后的数值列表。"""
        if pending[0] is None:
            return vals
        merged = list(vals)
        for i, v in enumerate(pending[0]):
            if i < len(merged) and merged[i] is None and v is not None:
                merged[i] = v
        pending[0] = None
        return merged

    page_rows = []
    for pg in pages:
        if pg["page"] < start_page:
            continue
        if stop_page is not None and pg["page"] > stop_page:
            break
        rows = _cluster_rows(pg["words"])
        page_rows.append((pg, rows))

    gutter_saved = None  # 双栏排版的栏间分割线（表头仅出现在首页，后续页沿用）
    col_mode = None      # 数值列布局（ytd/entity，表头仅在首页，续页沿用）
    for pg, rows in page_rows:
        if col_mode is None:
            col_mode = _header_mode(rows)
        zone = _footer_zone(rows)
        rows = [r for r in rows if not _is_footer_row(r, zone)]
        gutter = _find_gutter(rows)
        if gutter is not None:
            gutter_saved = gutter
        elif gutter_saved is not None:
            gutter = gutter_saved
        if pg["page"] == start_page:
            started = True  # 起始页由报表标题定位，直接开始
            # 起始页页首可能残留上一张报表的尾部行（如利润表首页含资产负债表尾行），
            # 以表头行为锚点，仅解析其后的科目行，并按该部分重新识别数值列。
            hi = _find_header_index(rows)
            if hi >= 0:
                rows = rows[hi + 1:]
        # 母公司报表标题为解析边界：截断其后的行，避免母公司报表污染数值列识别，
        # 并使后续页（纯母公司表内容）不再被解析。
        pi = _find_parent_index(rows)
        hit_boundary = pi >= 0
        if hit_boundary:
            rows = rows[:pi]
        # 左右双栏排版（左栏资产、右栏负债）按分栏线拆开分别解析，
        # 否则两栏的科目名与金额会被拼成一行，导致科目名污染、数值列错位。
        segments = [rows]
        if gutter is not None and _both_sides_have_nums(rows, gutter):
            segments = _split_blocks(rows, gutter)
        for seg in segments:
            if not seg:
                continue
            value_cols = _value_columns(seg, mode=col_mode)
            for row in seg:
                text_words = [w for w in row if not _NUM_RE.match(w["text"])]
                num_words = [w for w in row if _NUM_RE.match(w["text"])]
                text = "".join(w["text"] for w in sorted(text_words, key=lambda w: w["x0"]))
                # 数值按「最近的数值列」归位：某期单元格空白（或印「-」）时不会把
                # 后一期金额顶到前一列，避免本期/上期错位。
                if value_cols:
                    vals = [None] * len(value_cols)
                    for w in sorted(num_words, key=lambda w: w["x0"]):
                        cx = (w["x0"] + w["x1"]) / 2
                        dist, idx = min(
                            (min(abs(c["center"] - cx), abs(c["right"] - w["x1"])), i)
                            for i, c in enumerate(value_cols)
                        )
                        if dist <= 20 and vals[idx] is None:
                            vals[idx] = _parse_num(w["text"])
                    nums = [v for v in vals if v is not None]
                else:
                    vals = [_parse_num(w["text"]) for w in sorted(num_words, key=lambda w: w["x0"])]
                    nums = vals
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
                    # 纯数值行：名称与数值分行时，数值归入当前科目；
                    # 当前科目已取满时视为「下一科目的孤行金额」，暂存待合并。
                    if cur is not None and cur["vals"] is None:
                        cur["vals"] = absorb(vals)
                        _merge_bbox(cur["meta"], row_meta, "")
                    elif any(v is not None and (abs(v) >= 1000 or not float(v).is_integer())
                             for v in vals):
                        pending[0] = vals
                    continue

                if text and nums:
                    vals = absorb(vals)
                    if cur is not None and cur["vals"] is None:
                        merged = _merge_name(cur["name"], text)
                        if merged is not None:
                            cur["name"] = merged
                            cur["vals"] = vals
                            _merge_bbox(cur["meta"], row_meta, text)
                        else:
                            flush()
                            cur = {"name": text, "vals": vals, "meta": new_meta(row_meta, text)}
                    else:
                        flush()
                        cur = {"name": text, "vals": vals, "meta": new_meta(row_meta, text)}
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

                # 结束锚点（如「负债和所有者权益总计」）：本表解析完成
                if text and any(marker in normalize_account(text) for marker in end_set):
                    flush()
                    stopped = True
                    break
        if stopped or hit_boundary:
            break

    flush()
    return rows_data, unknown, blanks


def _to_dataframe(rows_data: list, stmt: str) -> pd.DataFrame:
    if stmt == "balance_sheet":
        cols = ["科目", "本期期末", "上期期末"]
    else:
        cols = ["科目", "本期金额", "上期金额"]
    records = [{"科目": r["name"], cols[1]: r["本期"], cols[2]: r["上期"]} for r in rows_data]
    return pd.DataFrame(records, columns=cols)


def _extract_meta(pages: list) -> dict:
    """提取公司名与报告期（年度报告 / 半年度报告 / 第X季度报告）。

    封面页常为美术设计页（文字极少或公司名缺「股份有限公司」后缀），
    故在前若干页中查找，直到公司名与报告期都取到为止。
    """
    year = ""
    period = ""
    company = ""
    for pg in pages[:12]:
        text = pg["text"]
        if not (year and period):
            p = _PERIOD_RE.search(text)
            if p:
                year = p.group(1)
                raw = p.group(2) or "年度"
                if raw == "中期":
                    period = "半年度报告"
                elif raw in ("年度", "度"):
                    period = "年度报告"
                else:  # 半年度 / 第一季度 / 第三季度
                    period = raw + "报告"
        if not company:
            c = _COMPANY_RE.search(text)
            if c:
                company = _clean_company(c.group(1), "释义" in text)
        if company and year:
            break
    if not year:
        # 封面年份可能写作「二〇二六」或散落在前几页正文中
        for pg in pages[:12]:
            m = re.search(r"(20\d{2})\s*年", pg["text"])
            if m:
                year = m.group(1)
                break
    if not year:
        for pg in pages[:12]:
            m = _CN_YEAR_RE.search(pg["text"])
            if m:
                year = "".join(_CN_DIGIT.get(c, "") for c in m.group(1))
                break
    if not period:
        for pg in pages[:12]:
            for kw, label in _PERIOD_KWS:
                if kw in pg["text"]:
                    period = label
                    break
            if period:
                break
    return {"company": company, "year": year, "period": period or "年度报告"}


def _clean_company(name: str, glossary: bool = False) -> str:
    """清理公司名多余前缀。

    释义页常写成「简称指全称」的形式（如「隆平高科指袁隆平农业高科技股份有限公司」），
    只有在释义页才按「指」切分，避免误伤名称本身含「指」的公司（如「指南针」）。
    """
    name = name.strip(" ）)")
    if glossary and "指" in name:
        name = name.rsplit("指", 1)[1]
    return name.strip(" ）)")


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
        "blank_accounts": {"balance_sheet": [], "income": [], "cashflow": []},
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
        rows_data, unknown, blanks = _parse_statement(
            pages, start, END_MARKERS[stmt], stop_page=stop_for(start),
            stop_titles=_STOP_TITLES[stmt],
        )
        result[stmt] = _to_dataframe(rows_data, stmt)
        result["unknown_accounts"].extend(unknown)
        result["blank_accounts"][stmt] = sorted(blanks)
        for r in rows_data:
            trace = dict(r["trace"])
            trace["account"] = r["name"]
            trace["stmt"] = stmt
            trace["values"] = [r["本期"], r["上期"]]
            result["traces"][stmt].append(trace)

    return result
