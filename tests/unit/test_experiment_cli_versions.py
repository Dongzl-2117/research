"""自定义适配器配置身份必须在调用前明确；命令测试不启动服务或模型。"""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from researchlib.experiments import __main__ as cli
from researchlib.experiments.demo import demo_snapshot
from researchlib.experiments.store import Store


@pytest.mark.parametrize("arguments", [
    ["--generator", "synthetic:generate"],
    ["--generator", "synthetic:generate", "--generator-version", " "],
    ["--generator-version", "v1"],
    ["--site-model", "--generator-version", "v1"],
])
def test_cli_requires_version_before_importing_or_starting(arguments, monkeypatch):
    calls = []
    monkeypatch.setattr(cli.importlib, "import_module", lambda name: calls.append(name))
    with pytest.raises(SystemExit) as error:
        cli.main(["serve", "--snapshot", "not-read.json", "--db", "not-opened.sqlite3", *arguments])
    assert error.value.code == 2 and calls == []


def test_cli_versions_bind_same_function_to_distinct_comparisons(tmp_path, monkeypatch):
    import uvicorn

    snapshot = demo_snapshot()
    path = tmp_path / "snapshot.json"
    path.write_text(snapshot.model_dump_json())
    db = tmp_path / "experiments.sqlite3"
    Store.initialize(db, snapshot.fingerprint)
    served, model_calls = [], []
    def generate(**kwargs):
        model_calls.append(kwargs)
        raise AssertionError("仅构造和比较不得调用模型")
    monkeypatch.setattr(cli.importlib, "import_module", lambda name: SimpleNamespace(generate=generate))
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: served.append(app))
    ids = []
    for version in ("v1", "v1", "v2"):
        cli.main(["serve", "--snapshot", str(path), "--db", str(db), "--generator", "synthetic:generate",
                  "--generator-version", version])
        app = served[-1]
        with TestClient(app, base_url="http://127.0.0.1:8100", client=("127.0.0.1", 123)) as client:
            client.headers["x-experiment-csrf"] = app.state.experiment_csrf
            assert client.get("/api/experiments/state").json()["generator"] == f"synthetic:generate@{version}"
            response = client.post("/api/experiments/compare", json={"case_ids": ["demo-1"]})
            assert response.status_code == 200, response.text
            ids.append(response.json()["id"])
    assert ids[0] == ids[1] and ids[0] != ids[2] and model_calls == []
