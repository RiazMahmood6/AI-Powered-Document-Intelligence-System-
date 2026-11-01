import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app          # noqa: E402
from models import db               # noqa: E402


@pytest.fixture()
def app(tmp_path):
    app = create_app({
        "TESTING": True, "JOBS_SYNC": True,
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'test.db'}",
        "UPLOAD_DIR": str(tmp_path / "uploads"),
        "LLM_API_KEY": None, "EXTRACTION_MODE": "heuristic", "API_KEY": None,
    })
    yield app
    with app.app_context():
        db.session.remove()
        db.engine.dispose()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def upload(client):
    def _upload(path, name=None):
        with open(path, "rb") as f:
            return client.post("/api/documents", data={"files": (f, name or os.path.basename(path))},
                               content_type="multipart/form-data")
    return _upload
