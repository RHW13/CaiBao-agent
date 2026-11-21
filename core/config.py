"""环境变量与 LLM 客户端配置。"""
import os
from functools import lru_cache

try:
    from dotenv import load_dotenv

    load_dotenv()  # 加载项目根目录 .env（若存在）
except ImportError:  # python-dotenv 未安装时忽略
    pass


def _get(name: str, default: str = "") -> str:
    """读取配置：优先环境变量，其次 Streamlit Cloud 的 secrets。

    Streamlit Cloud 的 Secrets 不会自动注入为环境变量，需显式读取 st.secrets；
    本地没有 secrets 文件时 st.secrets 会抛异常，此处静默回退。
    """
    value = os.getenv(name)
    if not value:
        try:
            import streamlit as st

            value = st.secrets.get(name)
        except Exception:
            value = None
    return (value or default).strip()


@lru_cache(maxsize=1)
def get_llm_api_key() -> str:
    """读取 DeepSeek API Key，未配置返回空字符串。"""
    return _get("DEEPSEEK_API_KEY")


def get_llm_base_url() -> str:
    return _get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")


def get_llm_model() -> str:
    return _get("DEEPSEEK_MODEL", "deepseek-chat")


def get_llm_client():
    """返回 OpenAI SDK 客户端；未配置 Key 时返回 None。"""
    key = get_llm_api_key()
    if not key:
        return None
    try:
        from openai import OpenAI

        return OpenAI(api_key=key, base_url=get_llm_base_url())
    except ImportError:
        return None
