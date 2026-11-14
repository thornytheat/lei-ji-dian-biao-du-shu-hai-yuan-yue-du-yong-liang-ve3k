"""pytest 公共夹具：数据目录指向临时目录，避免污染项目目录；客户端启动时执行幂等 seed。"""

from __future__ import annotations

import os
import tempfile

os.environ["METERCALC_DATA_DIR"] = tempfile.mkdtemp(prefix="metercalc-test-")
os.environ.pop("METERCALC_DB", None)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import seed  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def sample():
    return seed.load_sample_files()
