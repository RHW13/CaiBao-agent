# 财报分析智能体

上传 A 股上市公司年报 / 半年报 / 季报 PDF，自动解析三张财务报表，完成比率计算、指标溯源、
附注风险扫描、拐点预警、横向对标、归因预测、财报问答，并一键生成投研底稿（Word / PDF）。

设计原则：**LLM 只读不算**——所有数字由确定性代码计算，模型只负责读懂文字；答不出的问题
明确拒答，绝不编造。

## 功能清单

| Tab | 功能 |
|---|---|
| 报表预览 | 三表结构化结果、非标准科目清单、解析警告 |
| 财务比率(36项) | 偿债/营运/盈利/质量/市场价值五类指标，口径可切换 |
| 杜邦分析 | ROE 三因素拆解 |
| 溯源证据链 | 指标 → 页码 + 原文片段，低置信标注 |
| 自定义公式 | 自然语言或公式输入，全表联动重算 |
| 风险扫描 | 6 类附注风险信号 + Altman Z / F 分数 / 五维评分 |
| 拐点信号 | 5 条背离规则打分 + 历史回测 |
| 横向对标 | 多公司指标对比、均值/中位数/分位、雷达图 |
| 归因与预测 | 杜邦连环替代、毛利率瀑布、3 年预测、DCF |
| 财报问答 | 数字题确定性计算，文本题强制页码引用，无依据拒答 |
| 投研底稿 | 六章底稿汇编，Word / PDF 双导出 |

侧边栏「分析场景」（研究 / 选股 / 排雷 / 拐点）用于快速定位重点 Tab。

## 环境安装

需要 Python 3.10+（本项目在 3.12 上验证）。

```bat
py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt ^
    -i https://mirrors.aliyun.com/pypi/simple/
```

## 运行

**方式一（Windows）**：双击 `run_app.bat`，浏览器会自动打开（端口自动选择）。

**方式二（命令行）**：

```bat
.\.venv\Scripts\python.exe -m streamlit run app.py --server.port 0
```

> 不要用 IDE 的「运行 Python 文件」按钮，Streamlit 应用必须通过 `streamlit run` 启动。
> 若 8501 端口被占用，`--server.port 0` 会自动选一个空闲端口。

## 配置 LLM（可选）

复制 `.env.example` 为 `.env`，填入：

```
DEEPSEEK_API_KEY=sk-xxxx
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat
```

不配置也能使用：比率、评分、风险规则、拐点、底稿（规则结论）均可离线运行；
问答与结论将退回「原文摘录 / 规则模板」。

## 目录结构

```
app.py                 Streamlit 主入口（11 个 Tab）
run_app.bat            Windows 启动器
core/
  config.py            LLM 配置
  pdf_loader.py        PDF 文字/坐标提取、三表定位（跳过目录页）
  statements.py        三表结构化、金额列识别、期间识别
  mapping.py           科目同义词词典与归一
  calibers.py          口径配置
  ratios.py            36 项指标与数据装配
  evaluator.py         白名单公式求值器
  formula_llm.py       自然语言 → 公式
  pdf_index.py         数字 → 页码索引
  evidence.py          指标溯源证据链
  notes_scan.py        附注切分与检索
  risk.py              风险信号扫描
  scores.py            Altman Z / F 分数 / 五维评分
  trend.py             背离信号与回测
  benchmark.py         多公司对标
  attribution.py       杜邦归因与毛利率瀑布
  forecast.py          3 年预测与 DCF
  qa.py                语义问答
  draft.py             投研底稿汇编与 Word/PDF 导出
data/                  测试用年报 PDF（已在 .gitignore 中排除）
tests/                 pytest 冒烟测试
```

## 测试

```bat
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest tests -q
```

覆盖：三表定位与目录页排除、比率与关键科目、公式白名单、问答 20 类问题与拒答、底稿双导出。

## 常见问题

| 现象 | 原因与处理 |
|---|---|
| 提示「疑似扫描件（无文字层）」 | 本工具只支持电子版 PDF；扫描件需先 OCR |
| 部分指标显示「数据缺失」 | 该公司报表没有对应科目（如银行无存货/流动负债划分），属正常 |
| 三张表未定位 | 检查 PDF 是否为完整财报；目录页与审计报告页已自动跳过 |
| 非标准科目很多 | 已折叠展示；只有被指标公式引用的科目才影响计算 |
| 端口被占用 | 用 `--server.port 0` 自动选端口，或先关闭旧 Streamlit 进程 |
| PDF 导出失败 | 确认已安装 `reportlab`；否则可只导出 Word |

## 部署指南（GitHub + Streamlit Community Cloud）

### 一、安装并配置 Git

当前机器尚未安装 Git，任选一种方式：

```powershell
# 方式 A：winget（必须带两个 accept 参数，否则在自动化/非交互终端会报“未知错误”）
winget install --id Git.Git -e --source winget `
  --accept-package-agreements --accept-source-agreements --disable-interactivity

# 方式 B：下载安装包
# https://git-scm.com/download/win
```

安装后**新开一个终端**，验证并配置身份：

```powershell
git --version
git config --global user.name  "你的名字"
git config --global user.email "你的邮箱@example.com"
```

### 二、在本地创建仓库

```powershell
cd /d d:\财管智能体
git init -b main
git add .
git status          # 确认 .env、data/*.pdf、.venv/ 未被加入
git commit -m "feat: 财报分析智能体 Day1-10"
```

> `.gitignore` 已排除 `.env`、`.streamlit/secrets.toml`、`data/*.pdf`、`.venv/`，
> 因此 API Key 与年报样本都不会进入仓库。

### 三、在 GitHub 建仓并推送

1. 打开 <https://github.com/new>
   - Repository name：`caibao-agent`（自定）
   - 可见性：Private 或 Public 均可部署
   - **不要**勾选 "Add a README file / .gitignore / license"（本地已有文件）
   - 点击 **Create repository**
2. 按页面提示执行（把 `<你的用户名>` 换成实际账号）：

```powershell
git remote add origin https://github.com/<你的用户名>/caibao-agent.git
git push -u origin main
```

3. 认证方式（HTTPS 推送会要求输入用户名与密码）：
   - 密码处填写 **Personal Access Token**，不是账号密码
   - 生成位置：GitHub → Settings → Developer settings → Personal access tokens
     → Tokens (classic) → Generate new token，勾选 `repo`
   - 或安装 GitHub CLI 后执行 `gh auth login`，按提示完成浏览器登录

> 不想用命令行：也可在 GitHub 仓库页点击 **Add file → Upload files**，
> 把项目文件（不含 `.venv/`、`.env`、`data/*.pdf`）拖拽上传后提交。

### 四、部署到 Streamlit Community Cloud

1. 打开 <https://share.streamlit.io>，点击 **Continue with GitHub** 并授权。
2. 点击 **Create app** → 选择 **Deploy a public app from GitHub**（私有仓库需先授权）。
3. 填写：
   - Repository：`<你的用户名>/caibao-agent`
   - Branch：`main`
   - Main file path：`app.py`
   - （可选）Advanced settings → Python version 选择 **3.12**
4. 点击 **Deploy**，等待构建完成（首次约 2-5 分钟）。
5. 配置密钥：应用页 → **Settings → Secrets**，粘贴：

```toml
DEEPSEEK_API_KEY = "sk-xxxx"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"
```

6. 保存后点击 **Reboot app** 使密钥生效。之后打开公网链接即可上传财报并完成全流程分析。

> 代码已兼容两种配置来源：本地读 `.env`，云端自动读 `st.secrets`（见 `core/config.py`）。
> 未配置密钥也能部署，只是问答与结论会退回「原文摘录 / 规则模板」。

### 五、部署常见错误

| 现象 | 处理 |
|---|---|
| `ModuleNotFoundError` | 确认 `requirements.txt` 在仓库根目录、包名拼写正确，提交后 Reboot |
| 页面能开但问答/结论不走 LLM | Secrets 未配置或名称拼错；保存后必须 Reboot |
| `git` 不是内部或外部命令 | 未安装 Git，或安装后未重开终端 |
| `push` 报 403 / 认证失败 | 密码处需填 Personal Access Token，而非账号密码 |
| 上传大文件报内存错误 | Community Cloud 单应用内存约 1GB，建议 ≤50MB 的年报 |
| 构建很慢或失败 | 首次安装 pandas/plotly 较慢，可查看 Build logs 定位具体包 |

> `data/` 下的样本 PDF 不会上传，云端不需要它们；`tests/` 也无需在云端运行。
