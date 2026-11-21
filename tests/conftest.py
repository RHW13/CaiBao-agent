"""pytest 共享 fixture：样本年报只解析一次。"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.pdf_index import build_index  # noqa: E402
from core.ratios import build_data  # noqa: E402
from core.statements import load_pdf  # noqa: E402

DATA = os.path.join(ROOT, "data")


@pytest.fixture(scope="session")
def pdf_data():
    return load_pdf(os.path.join(DATA, "688551.pdf"))


@pytest.fixture(scope="session")
def pdf_159():
    return load_pdf(os.path.join(DATA, "159.pdf"))


@pytest.fixture(scope="session")
def index(pdf_data):
    return build_index(pdf_data)


@pytest.fixture(scope="session")
def data_map(pdf_data):
    return build_data(pdf_data)
