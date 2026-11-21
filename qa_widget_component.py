"""财报问答悬浮组件：把 qa_widget 静态目录注册为 Streamlit 自定义组件。

组件不可用时（例如 iframe 被浏览器策略阻止），页面其余功能不受影响，
问答仍可在「财报问答」Tab 中完成。
"""
from pathlib import Path

import streamlit.components.v1 as components

_DIR = Path(__file__).parent / "qa_widget"
_component = components.declare_component("qa_widget", path=str(_DIR))


def mount(messages: list, ready: bool, company: str = "", key: str = "qa_widget"):
    """挂载悬浮问答窗。

    messages: [{role, content, citations}]，由 Python 持有，组件只负责渲染。
    ready: 是否已解析财报（未解析时输入框禁用）。
    返回 {"action": "ask", "question": str, "nonce": str} 或 None。
    """
    return _component(
        messages=messages or [],
        ready=bool(ready),
        company=company or "",
        key=key,
        default=None,
    )
