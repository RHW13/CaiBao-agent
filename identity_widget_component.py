"""身份识别组件：把 identity_widget 静态目录注册为 Streamlit 自定义组件。

组件在浏览器 localStorage 中保存设备唯一标识与登录令牌，返回
{"device_id": str, "token": str}；组件不可见且不阻塞页面渲染。
"""
from pathlib import Path

import streamlit.components.v1 as components

_DIR = Path(__file__).parent / "identity_widget"
_component = components.declare_component("identity_widget", path=str(_DIR))


def mount(set_token: str = "", clear_token: bool = False, key: str = "identity_widget"):
    """挂载身份组件。

    set_token:   登录成功后下发的新令牌，组件会写入 localStorage；
    clear_token: True 时清除本地令牌（退出登录）。
    返回 {"device_id": str, "token": str} 或 None。
    """
    return _component(
        set_token=set_token or "",
        clear_token=bool(clear_token),
        key=key,
        default=None,
    )
