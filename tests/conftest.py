"""
Shared test setup.

The app keeps its database, config.json and documents under SARDINE_DATA_DIR,
and importing appcore runs database migrations and reads config.json at import
time. So before any test module is even collected, the run moves into a
throwaway folder, points SARDINE_DATA_DIR at it, writes a config.json there
from config.json.example, and builds a fresh empty database. Nothing a test
does can reach a real database or config, and a fresh clone with no
config.json can run the suite.

Outbound network connections are refused for the whole run.
"""

import contextlib
import io
import json
import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
_run = {}


def _write_config(folder: Path) -> None:
    config = json.loads((REPO / "config.json.example").read_text())
    config["secret_key"] = "tests-fixed-secret-key"
    (folder / "config.json").write_text(json.dumps(config))


def _build_database():
    import setup
    with contextlib.redirect_stdout(io.StringIO()):
        setup.create_database()


def pytest_configure(config):
    work = Path(tempfile.mkdtemp(prefix="sardinetracker-tests-"))
    _run.update(cwd=os.getcwd(), work=work, data_dir=os.environ.get("SARDINE_DATA_DIR"))
    os.chdir(work)
    os.environ["SARDINE_DATA_DIR"] = str(work)   # before db or appcore is imported
    _write_config(work)
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))

    _build_database()
    import db
    if Path(db.DB_FILE).resolve() != (work / "biotracking.db").resolve():
        pytest.exit("refusing to run: db.py would not use the throwaway database", returncode=3)

    def refuse(*args, **kwargs):
        raise ConnectionRefusedError("tests may not use the network")
    socket.socket.connect = refuse
    socket.create_connection = refuse

    # Importing app imports reminders, which starts the background scheduler
    # (medication reminders, ntfy alerts). Tests never want it running.
    from apscheduler.schedulers.background import BackgroundScheduler
    BackgroundScheduler.start = lambda self, *args, **kwargs: None


def pytest_unconfigure(config):
    if "cwd" in _run:
        os.chdir(_run["cwd"])
        if _run["data_dir"] is None:
            os.environ.pop("SARDINE_DATA_DIR", None)
        else:
            os.environ["SARDINE_DATA_DIR"] = _run["data_dir"]
        shutil.rmtree(_run["work"], ignore_errors=True)


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """An empty, fully migrated database of this test's own, for tests that write.

    db.DB_FILE is fixed when db.py is imported, so it is pointed at this
    test's folder directly.
    """
    import db
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(db, "DB_FILE", str(tmp_path / "biotracking.db"))
    _build_database()
    import appcore
    with contextlib.redirect_stdout(io.StringIO()):
        db.run_migrations()
        appcore._auto_migrate()
    return tmp_path / "biotracking.db"


@pytest.fixture
def app_client(fresh_db, monkeypatch):
    """A test client for the whole app, every route registered, on a fresh database.

    CSRF checks are off so a logged-out POST reaches the login gate rather
    than stopping at the CSRF check first; the gate is what these tests are
    about.
    """
    import app as app_module
    flask_app = app_module.app
    monkeypatch.setitem(flask_app.config, "TESTING", True)
    monkeypatch.setitem(flask_app.config, "WTF_CSRF_ENABLED", False)
    return flask_app.test_client()
