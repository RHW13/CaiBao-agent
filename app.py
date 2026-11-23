"""财报分析智能体 · Streamlit 主入口。

链路：上传年报/半年报/季报 → 解析三表 → 36 比率 → 杜邦 → 风险 → 拐点 → 问答 → 投研底稿。
"""
import os
import tempfile
from datetime import datetime

import pandas as pd
import streamlit as st

from core.statements import load_pdf
from core import history, identity
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
from core.draft import CHAPTERS, assemble, to_docx, to_markdown, to_pdf, tables_to_pdf
from core.config import get_llm_client
import ui_theme as ui
import qa_widget_component as qa_widget
import identity_widget_component as identity_widget

st.set_page_config(
    page_title="财报分析智能体",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)
ui.apply_theme()
st.session_state.setdefault("chat", [])

# ---------------- 身份识别：账号登录态 + 设备唯一标识 ----------------
# 身份识别与旧数据迁移属于增强能力：任一步失败都退回「访客设备」主体，
# 不让启动期异常阻断整个页面（也兼容热更新后旧模块仍驻留 sys.modules 的情况）。
_ident, _device_id, _token, _auth_user = {}, "", "", None
try:
    _ident = identity_widget.mount(
        set_token=st.session_state.pop("_issue_token", ""),
        clear_token=st.session_state.pop("_clear_token", False),
    ) or {}
    _device_id = str(_ident.get("device_id") or "").strip()
    _token = str(_ident.get("token") or "").strip()
    _auth_user = identity.user_from_token(_token)
except Exception:                    # 组件不可用：按设备主体继续运行
    st.session_state["_identity_degraded"] = True

if _auth_user:
    st.session_state["auth_user"] = _auth_user
    st.session_state["auth_token"] = _token
else:
    st.session_state.pop("auth_user", None)
    st.session_state.pop("auth_token", None)

# 主体在服务端由身份计算，前端不可指定，保证越权访问无从下手
SUBJECT = identity.subject(_auth_user, _device_id)
SUBJECT_LABEL = identity.subject_label(_auth_user, _device_id)

_migrate = getattr(history, "migrate_legacy", None)
if _device_id and callable(_migrate):
    try:
        _migrate(SUBJECT)
    except Exception:                # 迁移失败不影响正常使用
        pass

# 历史模块接口自检：热更新后若旧模块仍在内存中，宁可降级也不抛错
_HISTORY_API = ("list_records", "get_record", "load_bytes", "add_record",
                "delete_record", "clear_all", "record_status", "total_size")
HISTORY_OK = all(hasattr(history, name) for name in _HISTORY_API)

MAX_MB = 50
# 单次横向对标最多纳入的文件数（含当前已上传公司），避免批量上传占满内存
MAX_BENCH_FILES = 20

RISK_HELP = {
    "z": {
        "title": "Altman Z 评分标准",
        "body": """
**模型选择**

- 输入了每股市价且能取到股本时，使用上市制造业模型 **Z**（X4 用股权市值）；
- 未提供市值时，退化为非上市模型 **Z′**（X4 用账面净资产）。

**计算逻辑**

- Z = 1.2·X1 + 1.4·X2 + 3.3·X3 + 0.6·X4 + 1.0·X5
- Z′ = 0.717·X1 + 0.847·X2 + 3.107·X3 + 0.420·X4 + 0.998·X5

| 变量 | 含义 | 计算口径 |
| --- | --- | --- |
| X1 | 营运资本 / 总资产 | (流动资产合计 − 流动负债合计) / 资产总计 |
| X2 | 留存收益 / 总资产 | (盈余公积 + 未分配利润) / 资产总计 |
| X3 | 息税前利润 / 总资产 | (利润总额 + 利息费用) / 资产总计 |
| X4 | 股权价值 / 总负债 | 上市：市值 / 负债合计；非上市：所有者权益合计 / 负债合计 |
| X5 | 总资产周转率 | 营业收入 / 资产总计 |

**判定标准**

| 模型 | 安全 | 灰色 | 危险 |
| --- | --- | --- | --- |
| Z | Z > 2.99 | 1.81 ≤ Z ≤ 2.99 | Z < 1.81 |
| Z′ | Z′ > 2.90 | 1.23 ≤ Z′ ≤ 2.90 | Z′ < 1.23 |

任一变量无法计算时显示「数据不足」，不做缺失值猜测。
""",
    },
    "f": {
        "title": "Piotroski F 分数评分标准",
        "body": """
**计分规则**

9 项二元检查，每项满足记 1 分、不满足记 0 分；数据缺失的检查项**不计入分母**，
因此显示为「得分 / 实际可判定项数」，而不是固定 9 分。

| 检查项 | 通过条件 | 计算口径 |
| --- | --- | --- |
| ROA > 0 | 本期为正 | 净利润 / 平均总资产 |
| 经营现金流 > 0 | 本期为正 | 经营活动产生的现金流量净额 |
| ROA 同比提升 | 本期 > 上期 | 净利润 / 平均总资产，与上期比较 |
| 应计质量 | 现金流回报 > ROA | 经营现金流 / 平均总资产 > ROA |
| 长期负债率下降 | 本期 < 上期 | 非流动负债合计 / 资产总计 |
| 流动比率提升 | 本期 > 上期 | 流动资产合计 / 流动负债合计 |
| 未增发股本 | 两期股本一致 | 本期股本 = 上期股本 |
| 毛利率提升 | 本期 > 上期 | (营业收入 − 营业成本) / 营业收入 |
| 总资产周转率提升 | 本期 > 上期 | 营业收入 / 平均总资产 |

**解读**：7–9 分为财务状况稳健，4–6 分为中性，0–3 分为存在明显财务压力；
分母小于 9 时说明报表缺少上期数或相关科目，结论需结合缺失项判断。
""",
    },
    "dim": {
        "title": "五维总分评分标准",
        "body": """
**计分规则**

五个维度各 20 分，单维得分 = 20 × 通过项数 / 该维可判定项数；
数据缺失的检查项不参与该维度的分母，五项相加为总分（满分 100）。

| 维度 | 检查项（通过条件） |
| --- | --- |
| 偿债能力 | 资产负债率 ≤ 50%；流动比率 ≥ 1.2；速动比率 ≥ 0.8；利息保障倍数 ≥ 3 |
| 盈利能力 | ROE ≥ 8%；净利率 ≥ 3%；毛利率 ≥ 20%；ROA ≥ 3% |
| 营运能力 | 应收账款周转率 ≥ 4；存货周转率 ≥ 4；总资产周转率 ≥ 0.6 |
| 成长能力 | 营业收入同比 > 0；净利润同比 > 0；总资产同比 > 0 |
| 现金流质量 | 经营现金流 > 0；净现比 ≥ 0.8；销售收现比率 ≥ 0.9；现金流量比率 ≥ 0.1 |

**口径说明**

- 涉及「平均」的指标按 (本期 + 上期) / 2 计算；
- 银行、保险等报表没有流动资产、存货、营业成本时，对应检查项按数据缺失处理，
  不扣分，也不计为通过。
""",
    },
    "grade": {
        "title": "综合评级判定标准",
        "body": """
**判定逻辑**

综合评级由五维总分（0–100）直接映射，不额外加权：

| 总分区间 | 评级 | 含义 |
| --- | --- | --- |
| ≥ 80 | 优秀 | 五项能力均衡且多数指标达标 |
| 60 – 79.9 | 良好 | 整体稳健，个别维度存在短板 |
| 40 – 59.9 | 一般 | 多项指标未达标，需关注经营与偿债压力 |
| < 40 | 较差 | 多数维度不达标，财务风险较高 |

**注意**：评级仅依据公开报表的规则化打分，不含行业景气、公司治理等定性因素，
不构成投资建议；若总分接近分档边界，应结合明细表与风险信号综合判断。
""",
    },
}


def memo(key: str, factory):
    """会话内缓存重计算结果（避免每次点控件都重算附注检索/评分）。"""
    store = st.session_state.setdefault("_memo", {})
    if key not in store:
        store[key] = factory()
    return store[key]


def table_export_panel(prefix: str, page_name: str, tables: list, meta: dict) -> None:
    """页面级表格导出：勾选表格 → 生成标准 PDF → 保存到本地。

    tables：[{"name": 表名, "frame": DataFrame}]；prefix 为页面唯一键前缀。
    勾选、全选/取消全选与下载按钮集中放在页面顶部，三页交互保持一致。
    """
    tables = [t for t in tables if t.get("frame") is not None and len(t["frame"])]
    if not tables:
        return
    keys = [f"{prefix}_pick_{i}" for i in range(len(tables))]
    # 默认全选：进入页面即可直接点下载，勾选框用于按需取消
    for k in keys:
        st.session_state.setdefault(k, True)
    store = st.session_state.setdefault("_table_pdf", {})
    picked = [t for i, t in enumerate(tables) if st.session_state.get(keys[i])]

    st.markdown(f"### 表格下载 · {page_name}")
    st.caption("勾选要导出的表格（默认全选），点击「仅下载选中表格」生成 PDF，"
               "再点「保存 PDF 到本地」完成下载。")
    with st.container(border=True):
        head = st.columns([1, 1])
        if head[0].button("全选", key=f"{prefix}_all", width="stretch"):
            for k in keys:
                st.session_state[k] = True
            st.rerun()
        if head[1].button("取消全选", key=f"{prefix}_none", width="stretch"):
            for k in keys:
                st.session_state[k] = False
            st.rerun()

        cols = st.columns(min(3, len(tables)))
        for i, t in enumerate(tables):
            cols[i % len(cols)].checkbox(t["name"], key=keys[i])
        st.caption(f"已选 {len(picked)} / {len(tables)} 张表；未勾选的表格不会进入 PDF。")

        # 独立的一级操作按钮：只导出当前勾选的表格
        clicked = st.button(
            f"仅下载选中表格（已选 {len(picked)} 张）", type="primary",
            key=f"{prefix}_gen", width="stretch", disabled=not picked,
        )

        if clicked:
            bar = st.progress(0.15, text="正在整理表格…")
            try:
                company = meta.get("company") or "公司"
                period = f"{meta.get('year') or ''}{meta.get('period') or ''}"
                pdf = tables_to_pdf(
                    picked, title=f"{company} {page_name}",
                    subtitle=f"报告期：{period}" if period else "",
                )
                bar.progress(1.0, text="生成完成")
                store[prefix] = {
                    "bytes": pdf,
                    "name": f"{company}_{page_name}.pdf",
                    "sig": "|".join(t["name"] for t in picked),
                }
            except Exception as exc:  # 依赖缺失、版面异常等
                store.pop(prefix, None)
                st.error(f"生成失败：{exc}。请调整勾选后重试。")

        saved = store.get(prefix)
        sig = "|".join(t["name"] for t in picked)
        if saved and saved.get("sig") == sig:
            st.success(f"PDF 已生成：{saved['name']}。点击下方按钮保存到本地。")
            st.download_button(
                "保存 PDF 到本地", saved["bytes"], file_name=saved["name"],
                mime="application/pdf", key=f"{prefix}_save",
                type="primary", width="stretch",
            )


@st.cache_data
def parse_bytes(content: bytes) -> dict:
    """按文件内容缓存解析结果（避免切换页面/口径时重复解析 PDF）。"""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as f:
        f.write(content)
        tmp = f.name
    try:
        return load_pdf(tmp)
    except ParseError:
        raise
    except Exception as exc:  # 损坏 / 加密 / 非标准 PDF
        # 不向界面回显底层异常文本，避免泄露服务端临时路径与解析库内部细节；
        # 具体原因保留在异常链中，供日志与本地排查使用。
        raise RuntimeError("无法解析该 PDF。请确认文件完整、未加密且为原始下载件。") from exc
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


# ---------------- 上传历史 ----------------

class _HistoryFile:
    """把历史记录包装成与 UploadedFile 相同的最小接口。"""

    def __init__(self, record: dict, blob: bytes):
        self.name = record.get("name") or "历史文档.pdf"
        self.size = record.get("size") or len(blob)
        self._blob = blob

    def getvalue(self) -> bytes:
        return self._blob


def _apply_delete(rid: str) -> None:
    """删除单条记录并复位相关会话缓存（按钮回调）。"""
    if HISTORY_OK:
        history.delete_record(SUBJECT, rid)
    if st.session_state.get("history_active") == rid:
        st.session_state.pop("history_active", None)
        st.session_state.pop("draft", None)
        st.session_state.pop("draft_key", None)
    st.session_state.pop("_table_pdf", None)
    st.rerun()


@st.dialog("删除上传记录")
def confirm_delete(rid: str, name: str) -> None:
    """二次确认弹窗：确认后同时删除索引与本地 PDF 文件。"""
    st.write(f"确定删除「{name}」吗？该文档的本地缓存与分析记录会一并移除。")
    c1, c2 = st.columns(2)
    if c1.button("取消", key=f"hist_cancel_{rid}", width="stretch"):
        st.rerun()
    c2.button("确认删除", key=f"hist_confirm_{rid}", type="primary", width="stretch",
              on_click=_apply_delete, args=(rid,))


def _apply_clear(sig: str) -> None:
    """清空历史并复位相关会话缓存（按钮回调，避免弹窗内状态竞争）。"""
    if HISTORY_OK:
        history.clear_all(SUBJECT)
    # 当前打开的上传文件保持显示，但不同步回历史，直到用户更换文件
    st.session_state["_history_no_save_sig"] = sig or None
    st.session_state.pop("history_active", None)
    st.session_state.pop("draft", None)
    st.session_state.pop("draft_key", None)
    st.session_state.pop("_table_pdf", None)
    st.rerun()


@st.dialog("清空上传历史")
def confirm_clear(sig: str) -> None:
    """二次确认：清空全部记录与本地 PDF 缓存。"""
    count = len(history.list_records(SUBJECT)) if HISTORY_OK else 0
    st.write(f"将删除全部 {count} 条上传记录及本地 PDF 缓存，此操作不可恢复。"
             "当前正在查看的文档会保持打开，但不再保留在历史中。")
    c1, c2 = st.columns(2)
    if c1.button("取消", key="hist_clear_cancel", width="stretch"):
        st.rerun()
    c2.button("确认清空", key="hist_clear_confirm", type="primary", width="stretch",
              on_click=_apply_clear, args=(sig,))


def sidebar_identity() -> None:
    """侧边栏身份区：未登录可注册 / 登录，已登录可退出。"""
    user = st.session_state.get("auth_user")
    with st.sidebar.expander("账号已登录" if user else "身份与登录", expanded=not user):
        st.caption(f"当前主体：{SUBJECT_LABEL}")
        if st.session_state.get("_identity_degraded"):
            st.caption("身份组件暂不可用，已按设备身份继续运行。")
        if user:
            st.caption("历史记录已绑定到账号，退出后回到本设备身份。")
            if st.button("退出登录", key="logout_btn", width="stretch"):
                identity.logout(st.session_state.get("auth_token"))
                st.session_state["_clear_token"] = True
                st.session_state.pop("auth_user", None)
                st.session_state.pop("auth_token", None)
                st.session_state.pop("history_active", None)
                st.rerun()
            return

        tab_login, tab_reg = st.tabs(["登录", "注册"])
        with tab_login, st.form("login_form"):
            lu = st.text_input("用户名", key="login_user")
            lp = st.text_input("密码", type="password", key="login_pwd")
            if st.form_submit_button("登录", width="stretch"):
                ok, res = identity.login(lu, lp)
                if ok:
                    st.session_state["_issue_token"] = res
                    st.rerun()
                st.error(res) if not ok else None
        with tab_reg, st.form("register_form"):
            ru = st.text_input("用户名", key="reg_user", help="字母、数字、下划线、点、短横线、@ 或中文")
            rp = st.text_input("密码（至少 6 位）", type="password", key="reg_pwd")
            rp2 = st.text_input("确认密码", type="password", key="reg_pwd2")
            if st.form_submit_button("注册", width="stretch"):
                if rp != rp2:
                    st.error("两次输入的密码不一致。")
                else:
                    ok, res = identity.register(ru, rp)
                    st.success(res) if ok else st.error(res)
        st.caption("未登录时按设备标识隔离；登录后历史归属到账号，换设备登录同样可见。")


def history_page() -> None:
    """当前身份主体的专属历史记录页：列表、时间筛选、逐条删除。"""
    role = "账号" if st.session_state.get("auth_user") else "设备"
    st.markdown("## 我的历史记录")
    if not HISTORY_OK:
        st.warning("历史记录模块版本不匹配，请重启应用（Reboot app）后再试。")
        return
    st.caption(f"当前身份：{SUBJECT_LABEL}（按{role}维度隔离）。"
               "此处仅列出并允许操作该身份上传的财报，其他用户/设备的数据不可见。")

    records = history.list_records(SUBJECT, limit=1000)
    if not records:
        st.info("当前身份暂无上传记录。切换到「财报分析」上传财报后会自动出现在这里。")
        return

    dates = sorted({r.get("uploaded_at", "")[:10] for r in records if r.get("uploaded_at")})
    if dates:
        picked = st.date_input(
            "时间范围",
            value=(datetime.strptime(dates[0], "%Y-%m-%d").date(),
                   datetime.strptime(dates[-1], "%Y-%m-%d").date()),
            key="hist_range",
        )
        if isinstance(picked, (tuple, list)):
            start, end = (picked[0], picked[-1]) if picked else (None, None)
        else:
            start = end = picked
        filtered = [
            r for r in records
            if start and start.isoformat() <= (r.get("uploaded_at", "")[:10] or "0000")
            <= end.isoformat()
        ]
    else:
        start = end = None
        filtered = records

    m1, m2, m3 = st.columns(3)
    m1.metric("记录数", f"{len(filtered)} / {len(records)}")
    m2.metric("文件总大小", f"{history.total_size(filtered) / 1024 / 1024:.2f} MB")
    m3.metric("时间跨度", f"{start} ~ {end}" if start else "—")

    if not filtered:
        st.warning("所选时间范围内没有记录。")
        return

    st.dataframe(
        pd.DataFrame([
            {
                "上传时间": r.get("uploaded_at", ""),
                "文件名称": r.get("name", ""),
                "大小(MB)": round(int(r.get("size") or 0) / 1024 / 1024, 2),
                "上传状态": history.record_status(SUBJECT, r),
                "公司": r.get("company", ""),
            }
            for r in filtered
        ]),
        width="stretch", hide_index=True,
    )

    st.markdown("#### 逐条操作")
    for r in filtered:
        rid = r["id"]
        status = history.record_status(SUBJECT, r)
        with st.container(border=True):
            c1, c2, c3 = st.columns([5, 1, 1], vertical_alignment="center")
            c1.markdown(
                f"**{r.get('name', '未命名.pdf')}**\n\n"
                f"{r.get('uploaded_at', '')} · {int(r.get('size') or 0) / 1024 / 1024:.2f} MB · {status}"
            )
            if c2.button("打开", key=f"page_open_{rid}", width="stretch",
                         disabled=status != history.STATUS_OK):
                st.session_state["history_active"] = rid
                st.session_state["nav_view"] = "财报分析"
                st.rerun()
            if c3.button("删除", key=f"page_del_{rid}", width="stretch"):
                confirm_delete(rid, r.get("name", ""))

    if st.button("清空我的全部记录", key="page_clear"):
        confirm_clear("")


def sidebar_history(uploaded=None) -> None:
    """侧边栏上传历史（紧凑布局）：标题与清空按钮同行，列表滚动查看。"""
    if not HISTORY_OK:
        st.sidebar.caption("历史记录模块版本不匹配，重启应用后可用。")
        return
    records = history.list_records(SUBJECT)
    sig = f"{uploaded.name}|{uploaded.size}" if uploaded is not None else ""
    title_col, action_col = st.sidebar.columns([2.4, 1], gap="small",
                                               vertical_alignment="center")
    title_col.markdown('<div class="cb-side-title">上传历史</div>',
                       unsafe_allow_html=True)
    if records and action_col.button("清空", key="hist_clear", width="stretch",
                                     help="清空所有上传记录"):
        confirm_clear(sig)
    if not records:
        st.sidebar.markdown(
            '<div class="cb-hist-empty"><b>暂无上传记录</b>'
            "<span>上传的财报会自动保存在这里。</span></div>",
            unsafe_allow_html=True,
        )
        return

    st.sidebar.caption("点击切换 · 最多 12 份")
    active = st.session_state.get("history_active")
    with st.sidebar.container(height=200, border=False):
        for record in records:
            rid = record["id"]
            with st.container(border=True):
                c1, c2 = st.columns([4.6, 1], gap="small", vertical_alignment="center")
                label = record.get("name") or "未命名.pdf"
                if len(label) > 24:
                    label = label[:22] + "…"
                if c1.button(label, key=f"hist_open_{rid}", width="stretch",
                             help="点击打开该文档的分析结果"):
                    st.session_state["history_active"] = rid
                    st.session_state["nav_view"] = "财报分析"
                    st.rerun()
                if c2.button("删除", key=f"hist_del_{rid}", width="stretch"):
                    confirm_delete(rid, record.get("name") or "")
                meta = " · ".join(x for x in (record.get("uploaded_at"),
                                              record.get("company")) if x)
                st.caption(("当前查看 · " if rid == active else "") + meta)


# ---------------- 侧边栏 ----------------
ui.sidebar_brand()
sidebar_identity()
_view = st.sidebar.radio(
    "导航", ["财报分析", "我的历史记录"], horizontal=True,
    key="nav_view", label_visibility="collapsed",
)

uploaded = None
if _view == "财报分析":
    uploaded = st.sidebar.file_uploader("上传年报 PDF", type=["pdf"])
    sidebar_history(uploaded)

if _view == "我的历史记录":
    history_page()
    st.stop()

# 新上传的文件优先；点击历史记录时切换为历史文档，直到再次上传新文件
if uploaded is not None:
    sig = f"{uploaded.name}|{uploaded.size}"
    if st.session_state.get("_upload_sig") != sig:
        st.session_state["_upload_sig"] = sig
        st.session_state.pop("history_active", None)
        st.session_state.pop("_history_no_save_sig", None)

source = None
active = st.session_state.get("history_active")
if HISTORY_OK and active:
    _record = history.get_record(SUBJECT, active)
    _blob = history.load_bytes(SUBJECT, active) if _record else None
    if _record and _blob is not None:
        source = _HistoryFile(_record, _blob)
    else:
        st.session_state.pop("history_active", None)
if source is None and uploaded is not None:
    source = uploaded

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

if source is None:
    ui.landing()
    qa_widget.mount(st.session_state.get("chat", []), ready=False)
    st.stop()

if source.size > MAX_MB * 1024 * 1024:
    st.error(f"文件 {source.size / 1024 / 1024:.1f}MB 超过 {MAX_MB}MB 上限，请压缩后再上传。")
    st.stop()

file_key = f"{source.name}|{source.size}"
if st.session_state.get("draft_key") != file_key:
    st.session_state.pop("draft", None)
    st.session_state.pop("bench_results", None)
    st.session_state["draft_key"] = file_key
try:
    with st.spinner("解析财报…"):
        data = parse_bytes(source.getvalue())
except (ParseError, RuntimeError) as e:
    st.error(str(e))
    st.stop()

# 解析成功后写入上传历史（同内容文件自动去重，不改变首次上传时间）；
# 刚执行「清空所有记录」时，当前打开的文件不再写回，直到用户更换文件。
if HISTORY_OK and st.session_state.get("_history_no_save_sig") != f"{source.name}|{source.size}":
    history.add_record(SUBJECT, source.name, source.getvalue(), data.get("meta") or {})

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
        _result = qa_answer(_question, data, index=index, use_llm=_use_llm, caliber=caliber_map)
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
_ratio_key = (f"ratios|{file_key}|{quick_ratio}|{roe_caliber}"
              f"|{price}|{dividend}|{dep_amort}")
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

st.caption("提示：报表预览、财务比率、杜邦分析三个页签顶部均提供「表格下载」，"
           "勾选表格后可一键导出为 PDF。")
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
    sheet_tables = [
        {"name": label, "frame": df}
        for label, df in [
            ("资产负债表", data.get("balance_sheet")),
            ("利润表", data.get("income")),
            ("现金流量表", data.get("cashflow")),
        ]
        if df is not None
    ]
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
    table_export_panel("sheet", "报表预览", sheet_tables, meta)

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

    ratio_blocks = []
    for cat in ratios["板块"].drop_duplicates():
        sub = ratios[ratios["板块"] == cat]
        ratio_blocks.append({
            "name": cat,
            "frame": sub[["指标", "重要性", "数值", "单位", "口径", "溯源", "备注"]],
        })
    for block in ratio_blocks:
        st.subheader(block["name"])
        st.dataframe(
            block["frame"],
            width="stretch",
            hide_index=True,
            column_config={
                "数值": st.column_config.NumberColumn(format="%.4f"),
                "重要性": st.column_config.TextColumn(width="small"),
                "溯源": st.column_config.TextColumn(width="small"),
            },
        )
    table_export_panel("ratio", "财务比率", ratio_blocks, meta)

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
    table_export_panel("dupont", "杜邦分析", [{"name": "杜邦三因素分解", "frame": dpt}], meta)

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
    s2.metric("F 分数", f"{f['score']} / {f.get('denominator', 9)}")
    s3.metric("五维总分", f"{total:.0f} / 100")
    s4.metric("综合评级", grade(total))

    help_key = st.session_state.get("risk_help")
    h1, h2, h3, h4 = st.columns(4)
    for col, key, label in (
        (h1, "z", "评分说明 · Altman Z"),
        (h2, "f", "评分说明 · F 分数"),
        (h3, "dim", "评分说明 · 五维总分"),
        (h4, "grade", "评分说明 · 综合评级"),
    ):
        active = help_key == key
        if col.button(label, key=f"risk_help_{key}", width="stretch",
                      type="primary" if active else "secondary"):
            st.session_state["risk_help"] = None if active else key

    help_key = st.session_state.get("risk_help")
    if help_key:
        with st.container(border=True):
            head, close = st.columns([7, 1], vertical_alignment="center")
            head.markdown(f"**{RISK_HELP[help_key]['title']}**")
            if close.button("关闭", key="risk_help_close", width="stretch"):
                st.session_state["risk_help"] = None
                st.rerun()
            st.markdown(RISK_HELP[help_key]["body"])

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
    files = files or []
    if len(files) > MAX_BENCH_FILES:
        st.warning(f"一次最多对比 {MAX_BENCH_FILES} 家公司，已仅取前 {MAX_BENCH_FILES} 份。")
        files = files[:MAX_BENCH_FILES]
    results = {meta.get("company") or uploaded.name: data}
    for file in files:
        try:
            parsed = parse_bytes(file.getvalue())
        except (ParseError, RuntimeError) as e:
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
    scenario_fc = forecast(
        data,
        revenue_growth=base_forecast["revenue_growth"] + growth_adjust,
        profit_growth=base_forecast["profit_growth"] + growth_adjust,
    )
    rev_g_txt = ("数据缺失" if base_forecast["revenue_growth_missing"]
                 else f"{base_forecast['revenue_growth']:.2%}")
    profit_g_txt = ("数据缺失" if base_forecast["profit_growth_missing"]
                    else f"{base_forecast['profit_growth']:.2%}")
    st.caption(
        f"基准增速：营业收入 {rev_g_txt}，净利润 {profit_g_txt}；经营现金流按营业收入增速外推；预测期 3 年。"
    )
    st.dataframe(
        scenario_fc["rows"], width="stretch", hide_index=True,
        column_config={c: st.column_config.NumberColumn(format="%.2f")
                       for c in ("营业收入", "净利润", "经营现金流")},
    )

    dcf_result = dcf(data, wacc=wacc, terminal_growth=terminal_growth,
                     growth_adjust=growth_adjust)
    if dcf_result["ok"]:
        d1, d2, d3 = st.columns(3)
        d1.metric("企业价值（亿元）", f"{dcf_result['enterprise'] / 1e8:.2f}")
        d2.metric("股权价值（亿元）", f"{dcf_result['equity'] / 1e8:.2f}")
        d3.metric("每股价值（元）", "—" if (dcf_result["per_share"] is None
                  or dcf_result["per_share"] < 0)
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
        result = qa_answer(question, data, index=index, use_llm=use_llm_qa, caliber=caliber_map)
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
