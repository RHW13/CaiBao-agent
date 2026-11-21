"""界面主题：白 + 学术蓝的配色、页头与卡片组件。

所有视觉都通过 CSS 注入实现，不改动 Streamlit 组件逻辑，
因此升级 Streamlit 或功能迭代时不会牵连分析代码。
"""
import html

import streamlit as st

CSS = """
<style>
:root{
  --navy:#12345E; --navy-2:#1B4B86; --blue:#2C6FB5;
  --sky:#EEF4FC; --line:#DCE7F4; --text:#1B2432; --muted:#5B6B80;
  --gold:#C0A062; --radius:14px;
}
html, body, .stApp, [data-testid="stMarkdownContainer"]{
  font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
}
.stApp{
  background:
    radial-gradient(1100px 460px at 12% -12%, #E8F0FB 0%, rgba(255,255,255,0) 62%),
    linear-gradient(180deg,#FCFDFF 0%,#F5F9FE 100%);
  color:var(--text);
}
header[data-testid="stHeader"]{ background:transparent; }
#MainMenu, footer, [data-testid="stDecoration"]{ visibility:hidden; }
.block-container{ padding-top:1.5rem; padding-bottom:3.5rem; max-width:1480px; }

/* ---------- 品牌页头 ---------- */
.cb-hero{
  position:relative; overflow:hidden; border:1px solid var(--line); border-radius:18px;
  padding:36px 40px; margin-bottom:20px;
  background:linear-gradient(135deg,#FFFFFF 0%,#F4F8FF 58%,#E9F1FB 100%);
  box-shadow:0 12px 34px rgba(18,52,94,.07);
}
.cb-hero::before{
  content:""; position:absolute; inset:0 0 auto 0; height:4px;
  background:linear-gradient(90deg,var(--navy) 0%,var(--blue) 48%,var(--gold) 100%);
}
.cb-kicker{
  font-size:11px; font-weight:700; letter-spacing:.22em; text-transform:uppercase;
  color:var(--blue);
}
.cb-hero h1, .cb-co h1{
  font-family:"Songti SC","SimSun",Georgia,serif; color:var(--navy);
  letter-spacing:.02em; margin:10px 0 8px;
}
.cb-hero h1{ font-size:38px; line-height:1.25; }
.cb-hero p{ color:var(--muted); font-size:15px; line-height:1.8; margin:0; max-width:780px; }
.cb-tags{ margin-top:20px; display:flex; gap:8px; flex-wrap:wrap; }
.cb-tags span{
  font-size:12px; color:var(--navy-2); background:#fff; border:1px solid var(--line);
  padding:5px 12px; border-radius:999px;
}
.cb-co{ border-bottom:2px solid var(--line); padding-bottom:14px; margin-bottom:18px; }
.cb-co h1{ font-size:30px; }
.cb-co-meta{ display:flex; gap:10px; flex-wrap:wrap; margin-top:4px; }
.cb-co-meta span{
  font-size:12px; color:var(--navy-2); background:var(--sky);
  border:1px solid var(--line); border-radius:6px; padding:3px 9px;
}

/* ---------- 落地页步骤卡 ---------- */
.cb-steps{ display:grid; grid-template-columns:repeat(3,1fr); gap:16px; margin:6px 0 10px; }
.cb-step{
  background:#fff; border:1px solid var(--line); border-radius:var(--radius);
  padding:22px 22px 20px; transition:transform .18s ease, box-shadow .18s ease;
}
.cb-step:hover{ transform:translateY(-2px); box-shadow:0 10px 24px rgba(18,52,94,.08); }
.cb-step-no{
  font-family:Georgia,serif; font-size:13px; color:var(--gold);
  letter-spacing:.14em; font-weight:700;
}
.cb-step h3{ margin:8px 0 6px; font-size:17px; color:var(--navy); }
.cb-step p{ margin:0; font-size:13.5px; line-height:1.75; color:var(--muted); }
@media (max-width:860px){ .cb-steps{ grid-template-columns:1fr; } .cb-hero{ padding:26px 22px; } }

/* ---------- 指标卡 ---------- */
div[data-testid="stMetric"]{
  background:linear-gradient(180deg,#FFFFFF 0%,#F7FAFF 100%);
  border:1px solid var(--line); border-radius:var(--radius);
  padding:14px 18px; box-shadow:0 4px 14px rgba(18,52,94,.05);
}
div[data-testid="stMetric"] label p{
  font-size:12px !important; letter-spacing:.06em; color:var(--muted) !important;
}
div[data-testid="stMetricValue"]{
  font-family:Georgia,"Songti SC",serif; color:var(--navy);
}

/* ---------- 标签页 ---------- */
.stTabs [data-baseweb="tab-list"]{
  gap:4px; background:#F1F6FD; padding:6px; border-radius:12px;
  border:1px solid var(--line); overflow-x:auto;
}
.stTabs [data-baseweb="tab"]{
  height:36px; padding:0 15px; border-radius:8px; background:transparent;
  color:var(--navy-2); font-size:13.5px; white-space:nowrap;
}
.stTabs [aria-selected="true"]{
  background:#fff !important; color:var(--navy) !important;
  font-weight:600; box-shadow:0 2px 8px rgba(18,52,94,.10);
}
.stTabs [data-baseweb="tab-highlight"], .stTabs [data-baseweb="tab-border"]{ display:none; }

/* ---------- 侧边栏 ---------- */
section[data-testid="stSidebar"]{
  background:#F6FAFF; border-right:1px solid var(--line);
}
section[data-testid="stSidebar"] .block-container{ padding-top:1.4rem; }
.cb-brand{ display:flex; align-items:center; gap:10px; margin-bottom:2px; }
.cb-brand-mark{
  width:30px; height:30px; border-radius:8px; flex:none;
  background:linear-gradient(140deg,var(--navy) 0%,var(--blue) 100%);
  color:#fff; font-family:Georgia,serif; font-size:14px; font-weight:700;
  display:flex; align-items:center; justify-content:center;
}
.cb-brand-text{ font-size:15px; font-weight:700; color:var(--navy); letter-spacing:.03em; }
.cb-brand-sub{ font-size:11.5px; color:var(--muted); margin:2px 0 14px 40px; }

/* ---------- 上传区与按钮 ---------- */
[data-testid="stFileUploader"] section{
  border:1.5px dashed #B9CFE8; border-radius:var(--radius); background:#FDFEFF;
}
[data-testid="stFileUploader"] section:hover{ border-color:var(--blue); background:#F7FAFF; }
.stButton > button, .stDownloadButton > button{
  border-radius:9px; border:1px solid var(--line); font-weight:600;
  transition:transform .15s ease, box-shadow .15s ease;
}
.stButton > button[kind="primary"]{
  background:var(--navy); border-color:var(--navy);
}
.stButton > button:hover, .stDownloadButton > button:hover{
  transform:translateY(-1px); box-shadow:0 6px 16px rgba(18,52,94,.14);
}

/* ---------- 容器、提示与表格 ---------- */
div[data-testid="stVerticalBlockBorderWrapper"]{
  border-radius:var(--radius) !important; border-color:var(--line) !important;
  background:#FFFFFF;
}
div[data-testid="stExpander"]{ border-radius:10px; border-color:var(--line); }
div[data-testid="stAlert"]{ border-radius:10px; }
.stDataFrame, .stTable{ border-radius:10px; overflow:hidden; }
h2, h3{ color:var(--navy); letter-spacing:.01em; }
h2{ font-size:20px !important; margin-top:1.4rem !important; }
hr{ border-color:var(--line); }
@media (prefers-reduced-motion:reduce){
  *{ transition:none !important; animation:none !important; }
}
</style>
"""


def apply_theme():
    st.markdown(CSS, unsafe_allow_html=True)


def sidebar_brand():
    st.sidebar.markdown(
        '<div class="cb-brand">'
        '<div class="cb-brand-mark">财</div>'
        '<div class="cb-brand-text">财报分析智能体</div>'
        "</div>"
        '<div class="cb-brand-sub">年报 / 半年报 / 季报 · 三表解析与投研分析</div>',
        unsafe_allow_html=True,
    )


def landing():
    """未上传文件时的首屏：说明做什么、怎么用。"""
    st.markdown(
        '<div class="cb-hero">'
        '<div class="cb-kicker">Financial Statement Intelligence</div>'
        "<h1>财报分析智能体</h1>"
        "<p>上传 A 股上市公司年报、半年报或季报，自动完成三张报表的结构化解析、"
        "36 项财务比率、指标溯源、附注风险扫描与投研底稿生成。"
        "所有数字均由确定性代码计算，模型只负责理解文字。</p>"
        '<div class="cb-tags"><span>三表解析</span><span>比率与杜邦</span>'
        "<span>指标溯源</span><span>风险排雷</span>"
        "<span>拐点预警</span><span>投研底稿</span></div>"
        "</div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        '<div class="cb-steps">'
        '<div class="cb-step"><div class="cb-step-no">01 / 读取</div>'
        "<h3>上传财报</h3><p>支持不同行业、不同报告期。自动跳过目录页，"
        "识别合并与母公司报表，缺科目会如实标注而非猜测。</p></div>"
        '<div class="cb-step"><div class="cb-step-no">02 / 分析</div>'
        "<h3>逐层拆解</h3><p>从偿债、营运、盈利、现金质量到杜邦归因，"
        "每条结论都能回溯到具体页码与原文。</p></div>"
        '<div class="cb-step"><div class="cb-step-no">03 / 产出</div>'
        "<h3>形成底稿</h3><p>把体检结果、风险清单与估值汇编成六章底稿，"
        "可导出 Word / PDF 直接进入研究流程。</p></div>"
        "</div>",
        unsafe_allow_html=True,
    )
    st.info("在左侧上传 PDF 即可开始；未配置模型密钥时，除问答解释外的分析仍可正常运行。")


def company_header(company: str, year: str, period: str):
    """已解析公司的页头：公司与报告期，不使用标题级别抢占内容层级。"""
    meta = "　·　".join(x for x in (f"{year} {period}".strip(), "合并口径", "单位：元") if x)
    st.markdown(
        '<div class="cb-co">'
        '<div class="cb-kicker">Company Brief</div>'
        f"<h1>{html.escape(company)}</h1>"
        f'<div class="cb-co-meta"><span>{html.escape(meta)}</span></div>'
        "</div>",
        unsafe_allow_html=True,
    )
