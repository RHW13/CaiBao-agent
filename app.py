"""财报分析智能体 · Streamlit 主入口。

链路：上传年报/半年报/季报 → 解析三表 → 36 比率 → 杜邦 → 风险 → 拐点 → 问答 → 投研底稿。
"""
import os
import tempfile

import pandas as pd
import streamlit as st

from core.statements import load_pdf
from core.pdf_loader import ParseError
from core.pdf_index import build_index
from core.evidence import indicator_evidence, attach_evidence
from core.ratios import compute_ratios, build_data, dupont_analysis, dupont_roe
from core.formula_llm import nl_to_ast
from core.evaluator import evaluate
from core.risk import scan_risks
from core.scores import altman_z, f_score, five_dimension_scores, radar_chart, grade
from core.trend import trend_panel, backtest
from core.benchmark import compare, bar_chart, score_matrix
from core.attribution import dupont_attribution, gross_margin_breakdown, mdna_evidence
from core.forecast import forecast, dcf
from core.qa import answer as qa_answer
from core.draft import CHAPTERS, assemble, to_docx, to_markdown, to_pdf
from core.config import get_llm_client
import ui_theme as ui
import qa_widget_component as qa_widget

st.set_page_config(
    page_title="财报分析智能体",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)
ui.apply_theme()
st.session_state.setdefault("chat", [])

MAX_MB = 50


def memo(key: str, factory):
    """会话内缓存重计算结果（避免每次点控件都重算附注检索/评分）。"""
    store = st.session_state.setdefault("_memo", {})
    if key not in store:
        store[key] = factory()
    return store[key]


@st.cache_data
def parse_bytes(content: bytes) -> dict:
    """按文件内容缓存解析结果（避免切换页面/口径时重复解析 PDF）。"""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as f:
        f.write(content)
        tmp = f.name
    try:
        return load_pdf(tmp)
    except Exception as exc:  # 损坏 / 加密 / 非标准 PDF
        raise RuntimeError(f"无法解析该 PDF（{exc}）。请确认文件完整、未加密且为原始下载件。") from exc
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


# ---------------- 侧边栏 ----------------
ui.sidebar_brand()

uploaded = st.sidebar.file_uploader("上传年报 PDF", type=["pdf"])

scenario = st.sidebar.radio(
    "分析场景", ["研究", "选股", "排雷", "拐点"],
    help="研究＝全量分析；选股＝评分与估值；排雷＝风险与财务安全；拐点＝背离信号",
)

st.sidebar.subheader("口径设置")
quick_ratio = st.sidebar.selectbox(
    "速动比率口径", ["标准", "严格"],
    help="标准＝(流动资产−存货)÷流动负债；严格＝再扣预付款项与一年内到期的非流动资产",
)
roe_caliber = st.sidebar.selectbox(
    "ROE 口径", ["标准", "期末口径"],
    help="标准＝净利润÷平均股东权益；期末口径＝净利润÷期末股东权益",
)

st.sidebar.subheader("市场价值（选填）")
price = st.sidebar.number_input("每股市价（元）", min_value=0.0, value=0.0, step=0.1,
                                help="填入后可计算市盈率、市净率、市销率、股息率")
dividend = st.sidebar.number_input("每股股利（元）", min_value=0.0, value=0.0, step=0.01,
                                   help="填入后可计算每股股利、股利支付率")
dep_amort = st.sidebar.number_input("折旧摊销（元）", min_value=0.0, value=0.0, step=1000.0,
                                    help="取自现金流量表补充资料，用于现金营运指数")

if uploaded is None:
    ui.landing()
    qa_widget.mount(st.session_state.get("chat", []), ready=False)
    st.stop()

if uploaded.size > MAX_MB * 1024 * 1024:
    st.error(f"文件 {uploaded.size / 1024 / 1024:.1f}MB 超过 {MAX_MB}MB 上限，请压缩后再上传。")
    st.stop()

file_key = f"{uploaded.name}|{uploaded.size}"
if st.session_state.get("draft_key") != file_key:
    st.session_state.pop("draft", None)
    st.session_state["draft_key"] = file_key
try:
    with st.spinner("解析财报…"):
        data = parse_bytes(uploaded.getvalue())
except (ParseError, RuntimeError) as e:
    st.error(str(e))
    st.stop()

index = build_index(data)

extra = {
    "每股市价": price or None,
    "每股股利": dividend or None,
    "折旧摊销": dep_amort or None,
}
caliber_map = {"速动比率": quick_ratio, "净资产收益率(ROE)": roe_caliber}

meta = data.get("meta", {})
ui.company_header(
    meta.get("company") or "未识别公司",
    meta.get("year") or "",
    meta.get("period") or "年度报告",
)

qa_event = qa_widget.mount(
    st.session_state.get("chat", []),
    ready=True,
    company=meta.get("company") or "",
)
if qa_event and qa_event.get("action") == "ask":
    _nonce = str(qa_event.get("nonce") or "")
    _question = (qa_event.get("question") or "").strip()
    if _question and _nonce and _nonce != st.session_state.get("_qa_nonce"):
        st.session_state["_qa_nonce"] = _nonce
        _use_llm = st.session_state.get("qa_llm", get_llm_client() is not None)
        _result = qa_answer(_question, data, index=index, use_llm=_use_llm)
        st.session_state.chat.append({"role": "user", "content": _question})
        st.session_state.chat.append({
            "role": "assistant", "content": _result["answer"],
            "citations": _result["citations"],
        })
        st.rerun()

SCENES = {
    "研究": ("全量分析", "从解析到结论全流程走一遍。",
             ["报表预览", "财务比率(36项)", "杜邦分析", "归因与预测", "投研底稿"]),
    "选股": ("评分与估值", "看综合评分、盈利质量与 DCF 估值。",
             ["财务比率(36项)", "风险扫描", "归因与预测", "投研底稿"]),
    "排雷": ("风险与财务安全", "看 Altman Z、F 分数、风险信号与偿债指标。",
             ["风险扫描", "溯源证据链", "财务比率(36项)", "投研底稿"]),
    "拐点": ("背离信号", "看营收/应收/存货/现金流/合同负债的背离。",
             ["拐点信号", "财务比率(36项)", "归因与预测", "投研底稿"]),
}
_scene_title, _scene_hint, _scene_tabs = SCENES[scenario]
_ratio_key = f"ratios|{file_key}|{quick_ratio}|{roe_caliber}"
_quick = memo(_ratio_key, lambda: compute_ratios(data, caliber=caliber_map, extra=extra))
with st.container(border=True):
    st.markdown(f"**{scenario}模式 · {_scene_title}**")
    st.caption(_scene_hint + "　建议重点查看：" + "、".join(_scene_tabs))
    q1, q2, q3 = st.columns(3)
    _idx = _quick.set_index("指标")
    for _col, _name in ((q1, "净资产收益率(ROE)"), (q2, "资产负债率"), (q3, "销售毛利率")):
        _v = _idx["数值"].get(_name)
        _unit = _idx["单位"].get(_name, "")
        if pd.isna(_v):
            _col.metric(_name, "—")
        elif _unit == "%":
            _col.metric(_name, f"{_v:.2f}%")
        else:
            _col.metric(_name, f"{_v:.2f}{_unit}")

tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8, tab9, tab10, tab11 = st.tabs(
    ["报表预览", "财务比率(36项)", "杜邦分析", "溯源证据链", "自定义公式",
     "风险扫描", "拐点信号", "横向对标", "归因与预测", "财报问答", "投研底稿"]
)

with tab1:
    for w in data.get("warnings", []):
        st.warning(w)
    unknown = sorted(set(data.get("unknown_accounts", [])))
    if unknown:
        with st.expander(f"非标准科目（{len(unknown)} 项，未归一，原样保留）"):
            st.caption("这些科目名称不在内置词典中，已原样保留在三表里；"
                       "只有被指标公式引用的科目才会影响计算。")
            st.code("、".join(unknown), language=None)
    for label, df in [
        ("资产负债表", data.get("balance_sheet")),
        ("利润表", data.get("income")),
        ("现金流量表", data.get("cashflow")),
    ]:
        st.subheader(label)
        if df is None:
            st.write("未解析到该报表。")
        else:
            st.dataframe(df, width="stretch", hide_index=True)

with tab2:
    ratios = memo(_ratio_key, lambda: compute_ratios(data, caliber=caliber_map, extra=extra)).copy()
    ratios = attach_evidence(index, ratios, use_llm=False)
    ratios["备注"] = [
        f"{c}；{n}" if c and n else (c or n)
        for c, n in zip(ratios["置信"], ratios["备注"])
    ]
    ok = ratios["数值"].notna().sum()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("可计算指标", f"{ok} / {len(ratios)}")
    c2.metric("速动比率口径", quick_ratio)
    c3.metric("ROE 口径", roe_caliber)
    c4.metric("溯源待确认", int((ratios["置信"] != "").sum()))
    if ok < len(ratios):
        st.caption("未计算的指标需在左侧补充每股市价、每股股利或折旧摊销。")

    for cat in ratios["板块"].drop_duplicates():
        sub = ratios[ratios["板块"] == cat]
        st.subheader(cat)
        st.dataframe(
            sub[["指标", "重要性", "数值", "单位", "口径", "溯源", "备注"]],
            width="stretch",
            hide_index=True,
            column_config={
                "数值": st.column_config.NumberColumn(format="%.4f"),
                "重要性": st.column_config.TextColumn(width="small"),
                "溯源": st.column_config.TextColumn(width="small"),
            },
        )

with tab3:
    dpt = dupont_analysis(data)
    st.caption("ROE = 销售净利率 × 总资产周转率 × 权益乘数（本期按平均数口径；上期以期末数近似）")
    st.dataframe(
        dpt, width="stretch", hide_index=True,
        column_config={
            "本期": st.column_config.NumberColumn(format="%.4f"),
            "上期": st.column_config.NumberColumn(format="%.4f"),
            "变动": st.column_config.NumberColumn(format="%.4f"),
        },
    )
    info = dupont_roe(data)
    f = info["factors"]
    if info["product"] is not None:
        st.info(
            f"恒等式校验：{f['销售净利率']:.4%} × {f['总资产周转率']:.4f} × "
            f"{f['权益乘数']:.4f} = **{info['product']:.4%}**"
            f"　（直接计算 ROE = {info['roe']:.4%}）"
        )
    if f.get("销售净利率") is not None and f.get("总资产周转率") is not None:
        st.caption(
            "驱动类型判断：销售净利率反映盈利能力，总资产周转率反映营运能力，"
            "权益乘数反映财务杠杆；需结合同行业对比判断 ROE 的可持续性。"
        )

with tab4:
    st.caption("每个数字可追溯到 PDF 页码与原文；置信度低于 0.70 标记「需人工确认」，未定位不猜值。")
    sel = st.selectbox("选择指标", ratios["指标"].tolist(), key="evidence_indicator")
    ev_rows = indicator_evidence(index, sel, use_llm=True)

    found_n = sum(1 for r in ev_rows if r["found"])
    low_n = sum(1 for r in ev_rows if r["found"] and r["flagged"])
    m1, m2, m3 = st.columns(3)
    m1.metric("涉及科目", len(ev_rows))
    m2.metric("已定位", f"{found_n} / {len(ev_rows)}")
    m3.metric("需人工确认", low_n)

    table = pd.DataFrame([
        {
            "科目": r["科目"],
            "页码": "、".join(f"P{p}" for p in r["pages"]) if r["found"] else "未定位",
            "原文科目": r["text"],
            "本期数": r["values"][0] if r["values"] else None,
            "上期数": r["values"][1] if len(r["values"]) > 1 else None,
            "置信度": r["confidence"],
            "判定理由": r["reason"],
        }
        for r in ev_rows
    ])
    st.dataframe(
        table, width="stretch", hide_index=True,
        column_config={
            "本期数": st.column_config.NumberColumn(format="%.2f"),
            "上期数": st.column_config.NumberColumn(format="%.2f"),
            "置信度": st.column_config.NumberColumn(format="%.2f"),
        },
    )

    for r in ev_rows:
        if not r["found"]:
            if r.get("external"):
                st.info(f"「{r['科目']}」：{r['reason']}")
            else:
                st.warning(f"「{r['科目']}」：{r['reason']}")
            continue
        pages = "、".join(str(p) for p in r["pages"])
        flag = "　⚠ 需人工确认" if r["flagged"] else ""
        with st.expander(f"{r['科目']}　第 {pages} 页　置信度 {r['confidence']:.2f}{flag}"):
            st.caption(r["reason"])
            st.code(r["snippet"] or "（未取到上下文）", language=None)

with tab5:
    st.write("输入自然语言或公式，例如：`ROE = 净利润 / 平均净资产`、`毛利率 = (营业收入-营业成本)/营业收入`")
    formula = st.text_input("公式 / 自然语言")
    if formula:
        r = nl_to_ast(formula)
        if r["ast"] is None:
            st.error(r["hint"])
        else:
            d = build_data(data, extra)
            val = evaluate(r["ast"], d)
            source = "LLM" if r["source"] == "llm" else "规则模板"
            shown = r.get("formula") or formula
            st.write(f"解析来源：{source}　|　公式：{shown}")
            if val is None:
                st.warning("结果不可计算（数据缺失或分母为 0）。")
            else:
                st.metric("结果", f"{val:.4f}")
            if r.get("hint"):
                st.caption(r["hint"])

with tab6:
    st.caption(
        "附注章节切分 + 关键词检索 + 规则定级；LLM 抽取需手动开启，"
        "输出必须能在原文中逐字找到，否则丢弃。"
    )
    use_llm = st.toggle("启用 LLM 结构化抽取", value=False)
    risk = memo(f"risk|{file_key}|{use_llm}",
                lambda: scan_risks(data, index=index, use_llm=use_llm))
    signals = risk["signals"]

    r1, r2, r3 = st.columns(3)
    r1.metric("高危", sum(s["等级"] == "高危" for s in signals))
    r2.metric("中高 / 关注", sum(s["等级"] in ("中高", "关注") for s in signals))
    r3.metric("已扫描类型", f"{len(risk['checked'])} 类")
    if risk["llm_note"]:
        st.caption(risk["llm_note"])

    if signals:
        st.dataframe(
            pd.DataFrame([
                {"类型": s["类型"], "等级": s["等级"], "依据": s["依据"],
                 "页码": s["页码"] or "—", "来源": s["来源"]}
                for s in signals
            ]),
            width="stretch", hide_index=True,
            column_config={"等级": st.column_config.TextColumn(width="small")},
        )
        for s in signals:
            if not s["原文"]:
                continue
            with st.expander(f"{s['类型']}（{s['等级']}）· 第 {s['页码']} 页"):
                st.caption(s["依据"])
                st.code(s["原文"], language=None)
    else:
        st.info("未命中规则信号。")

    st.subheader("风险评分（M5）")
    vals = memo(f"valdata|{file_key}", lambda: build_data(data, extra))
    market_value = price * vals["股本"] if price and vals.get("股本") else None
    alt = altman_z(vals, market_value=market_value)
    f = f_score(vals)
    dim_scores, dim_detail = five_dimension_scores(vals)
    total = sum(dim_scores.values())

    s1, s2, s3, s4 = st.columns(4)
    s1.metric("Altman Z", "—" if alt["z"] is None else f"{alt['z']:.2f}", alt["zone"])
    s2.metric("F 分数", f"{f['score']} / 9")
    s3.metric("五维总分", f"{total:.0f} / 100")
    s4.metric("综合评级", grade(total))

    st.plotly_chart(radar_chart(dim_scores), width="stretch")
    with st.expander(f"Altman Z 分项（{alt['model']}）"):
        st.dataframe(
            pd.DataFrame([{"变量": k, "取值": v} for k, v in alt["parts"].items()]),
            width="stretch", hide_index=True,
        )
    with st.expander("Piotroski F 分数明细"):
        st.dataframe(pd.DataFrame(f["rows"]), width="stretch", hide_index=True)
    with st.expander("五维评分明细"):
        st.dataframe(pd.DataFrame(dim_detail), width="stretch", hide_index=True)

with tab7:
    st.caption("5 条背离规则独立打分；预警分 = 触发信号权重之和（满分 100）。")
    panel = memo(f"trend|{file_key}", lambda: trend_panel(data))
    t1, t2 = st.columns(2)
    t1.metric("预警分", f"{panel['score']} / 100")
    t2.metric("风险等级", panel["level"])
    st.progress(min(max(panel["score"], 0), 100) / 100.0)

    st.dataframe(
        pd.DataFrame([
            {"信号": s["名称"], "触发": "是" if s["触发"] else "否",
             "权重": s["权重"], "说明": s["说明"], "跟踪科目": s["跟踪"]}
            for s in panel["signals"]
        ]),
        width="stretch", hide_index=True,
    )
    st.caption("下一步跟踪：" + ("、".join(panel["track"]) if panel["track"] else "无"))

    bt = backtest(panel["series"])
    if bt["insufficient"]:
        st.info(
            f"历史回测：当前仅 {bt['periods']} 期，样本不足（需 ≥3 期），"
            "暂不计算命中率。上传同公司连续多期年报后可回测。"
        )
    else:
        st.dataframe(
            pd.DataFrame(bt["rows"]), width="stretch", hide_index=True,
            column_config={"命中率": st.column_config.NumberColumn(format="%.1%")},
        )
    st.dataframe(panel["series"], width="stretch", hide_index=True)

with tab8:
    st.caption("选择多份年报 PDF 一键对比（当前已上传的公司会自动纳入）。")
    files = st.file_uploader(
        "批量上传公司年报 PDF（可多选）", type=["pdf"],
        accept_multiple_files=True, key="bench_files",
    )
    results = {meta.get("company") or uploaded.name: data}
    for file in files or []:
        try:
            parsed = parse_bytes(file.getvalue())
        except ParseError as e:
            st.warning(f"{file.name}：{e}")
            continue
        name = parsed.get("meta", {}).get("company") or file.name
        results[name] = parsed

    if len(results) < 2:
        st.info("当前仅 1 家公司。请再上传至少 1 份年报（可一次多选）后查看对比。")
    else:
        st.session_state["bench_results"] = results
        with st.spinner("计算对标指标…"):
            detail, summary = compare(results)
        st.subheader("指标对比（本期）")
        st.dataframe(
            detail, width="stretch",
            column_config={c: st.column_config.NumberColumn(format="%.4f") for c in detail.columns},
        )
        st.subheader("均值 / 中位数 / 分位")
        st.dataframe(
            summary, width="stretch", hide_index=True,
            column_config={
                c: st.column_config.NumberColumn(format="%.4f")
                for c in ("均值", "中位数", "25分位", "75分位")
            },
        )
        indicator = st.selectbox("柱状图指标", detail.index.tolist(), key="bench_indicator")
        st.plotly_chart(bar_chart(detail, indicator), width="stretch")
        st.subheader("五维评分雷达")
        st.plotly_chart(radar_chart(score_matrix(results)), width="stretch")

with tab9:
    st.caption("杜邦三因素连环替代 + 毛利率瀑布 + 管理层讨论佐证 + 3 年情景预测。")

    attr = dupont_attribution(data)
    if not attr["ok"]:
        st.warning("归因所需数据缺失（需本期与上期营业收入、净利润、资产与权益）。")
    else:
        st.subheader("杜邦三因素归因")
        st.dataframe(
            pd.DataFrame([
                {"因素": r["因素"], "上期": r["上期"], "本期": r["本期"], "贡献": r["贡献"]}
                for r in attr["rows"]
            ]),
            width="stretch", hide_index=True,
            column_config={c: st.column_config.NumberColumn(format="%.4f")
                           for c in ("上期", "本期", "贡献")},
        )
        contribution = sum(r["贡献"] for r in attr["rows"])
        st.info(
            f"ROE {attr['roe_prev']:.2%} → {attr['roe_cur']:.2%}，"
            f"变动 {attr['change']:+.2%}；三因素贡献合计 {contribution:+.2%}"
        )

    margin = gross_margin_breakdown(data)
    if margin["ok"]:
        st.subheader("毛利率变动瀑布")
        st.plotly_chart(margin["figure"], width="stretch")
        st.caption(
            f"上期毛利率 {margin['gm_prev']:.2%} → 本期 {margin['gm_cur']:.2%}，"
            f"变动 {margin['change']:+.2%}（收入影响与成本影响按连环替代顺序拆分）"
        )

    st.subheader("管理层讨论与分析（公司解释）")
    st.caption("以下为原文摘录并标注页码；上方归因结果为系统计算推断。")
    evidence = mdna_evidence(data)
    if evidence:
        for item in evidence:
            with st.expander(f"第 {item['page']} 页 · 相关度 {item['score']}"):
                st.code(item["snippet"], language=None)
    else:
        st.info("未在管理层讨论与分析章节检索到相关原文。")

    st.subheader("3 年情景预测与简化 DCF")
    f1, f2, f3 = st.columns(3)
    growth_adjust = f1.slider("增速调整（百分点）", -10, 10, 0, 1) / 100.0
    wacc = f2.slider("WACC", 6, 16, 10, 1) / 100.0
    terminal_growth = f3.slider("永续增长率", 0, 5, 3, 1) / 100.0

    base_forecast = forecast(data)
    scenario = forecast(
        data,
        revenue_growth=base_forecast["revenue_growth"] + growth_adjust,
        profit_growth=base_forecast["profit_growth"] + growth_adjust,
    )
    st.caption(
        f"基准增速：营业收入 {base_forecast['revenue_growth']:.2%}，"
        f"净利润 {base_forecast['profit_growth']:.2%}；经营现金流按营业收入增速外推；预测期 3 年。"
    )
    st.dataframe(
        scenario["rows"], width="stretch", hide_index=True,
        column_config={c: st.column_config.NumberColumn(format="%.2f")
                       for c in ("营业收入", "净利润", "经营现金流")},
    )

    dcf_result = dcf(data, wacc=wacc, terminal_growth=terminal_growth,
                     growth_adjust=growth_adjust)
    if dcf_result["ok"]:
        d1, d2, d3 = st.columns(3)
        d1.metric("企业价值（亿元）", f"{dcf_result['enterprise'] / 1e8:.2f}")
        d2.metric("股权价值（亿元）", f"{dcf_result['equity'] / 1e8:.2f}")
        d3.metric("每股价值（元）", "—" if dcf_result["per_share"] is None
                  else f"{dcf_result['per_share']:.2f}")
        st.caption(
            f"预测增速（基于营业收入同比）{dcf_result['growth']:.2%}；基期 FCF（经营现金流）"
            f"{dcf_result['base_fcf'] / 1e8:.2f} 亿元；净债务 {dcf_result['net_debt'] / 1e8:.2f} 亿元。"
        )
        if dcf_result["warning"]:
            st.warning(dcf_result["warning"])
    else:
        st.warning(dcf_result["reason"])
    st.caption(
        "说明：未引入 Prophet，按最新同比增速做确定性外推；DCF 以经营现金流近似 FCF，"
        "仅用于情景比较，不构成投资建议。"
    )

with tab10:
    st.caption("数字题由系统直接计算；文本题检索原文并强制标注页码；无依据时拒答。")
    company_key = meta.get("company") or uploaded.name
    if st.session_state.get("chat_company") != company_key:
        st.session_state.chat = []
        st.session_state.chat_company = company_key

    use_llm_qa = st.toggle(
        "启用 LLM 生成（关闭则仅返回原文摘录）",
        value=get_llm_client() is not None, key="qa_llm",
    )
    if st.button("清空对话"):
        st.session_state.chat = []

    for msg in st.session_state.get("chat", []):
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            for citation in msg.get("citations", []):
                with st.expander(f"第 {citation['page']} 页"):
                    st.code(citation["snippet"], language=None)

    question = st.chat_input(
        "例如：2025年营业收入是多少？ROE是多少？净利润下降的原因？"
    )
    if question:
        result = qa_answer(question, data, index=index, use_llm=use_llm_qa)
        st.session_state.chat.append({"role": "user", "content": question})
        st.session_state.chat.append({
            "role": "assistant", "content": result["answer"],
            "citations": result["citations"],
        })
        st.rerun()

with tab11:
    st.caption("把比率、评分、风险、拐点、对标与估值汇编成标准投研底稿；"
               "LLM 只撰写结论，结论中的数字需能回查到底稿数据。")
    chapters = st.multiselect("包含章节", CHAPTERS, default=CHAPTERS, key="draft_chapters")
    use_llm_draft = st.toggle(
        "用 LLM 撰写结论", value=get_llm_client() is not None, key="draft_llm",
        help="关闭则使用规则模板生成结论",
    )
    bench_data = st.session_state.get("bench_results")
    if bench_data:
        st.caption(f"对标章节将并入 {len(bench_data)} 家公司。")

    if st.button("生成底稿", type="primary"):
        with st.spinner("汇编底稿…"):
            st.session_state["draft"] = assemble(
                data, results=bench_data, extra=extra, index=index, use_llm=use_llm_draft,
            )

    draft = st.session_state.get("draft")
    if draft is None:
        st.info("点击「生成底稿」后在此预览并下载。")
    else:
        st.markdown(to_markdown(draft, chapters))
        d1, d2 = st.columns(2)
        base_name = (meta.get("company") or "公司") + "_投研底稿"
        d1.download_button(
            "下载 Word", to_docx(draft, chapters), file_name=f"{base_name}.docx",
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        try:
            d2.download_button(
                "下载 PDF", to_pdf(draft, chapters), file_name=f"{base_name}.pdf",
                mime="application/pdf",
            )
        except RuntimeError as e:
            d2.info(str(e))
