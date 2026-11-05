"""仅浏览器验收：主站挂载＋合成会话/模型，无生产认证或网络调用。"""
import json
import os
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

import yaml
from fastapi import Request

from researchlib import llm
from researchlib.config import Settings
from researchlib.experiments.demo import demo_snapshot
from researchlib.experiments.store import Store
from researchlib.web.app import create_app
from researchlib.web.experiment_access import ExperimentOwner
from researchlib.web.routes import render_page

folder = Path(os.environ["EXPERIMENT_SMOKE_DIR"])
snapshot = demo_snapshot()
(folder / "snapshot.json").write_text(snapshot.model_dump_json())
(folder / "sources.yaml").write_text(yaml.safe_dump({"sources": [asdict(s) for s in snapshot.sources]}))
Store.initialize(folder / "synthetic.sqlite3", snapshot.fingerprint)
settings = Settings.from_env({
    "DATA_DIR": str(folder), "PUBLIC_BASE_URL": os.environ["EXPERIMENT_SYNTHETIC_ORIGIN"],
    "SOURCES_FILE": str(folder / "sources.yaml"), "RESEARCH_EXPERIMENTS_ENABLED": "1",
    "RESEARCH_EXPERIMENTS_SNAPSHOT": str(folder / "snapshot.json"),
    "RESEARCH_EXPERIMENTS_DB": str(folder / "synthetic.sqlite3"), "RESEARCH_EXPERIMENTS_SITE_MODEL": "1",
})
owner = ExperimentOwner(
    "synthetic-browser-owner", "synthetic-browser-session", frozenset(s.id for s in snapshot.sources),
)


async def fake_verifier(request):
    return owner if request.headers.get("x-test-session") == "synthetic-browser-only" else None


calls = []
llm._deepseek_token = lambda: "synthetic-offline-only"


class FakeResponse:
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self, n):
        return json.dumps({
            "model": "synthetic-site-model", "choices": [{"finish_reason": "stop", "message": {
                "content": "合成替身回答，只依据原文。[1]",
            }}], "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        }).encode()[:n]


def fake_open(self, request, timeout=None):
    payload = json.loads(request.data)
    calls.append(payload)
    if "SIMULATE-FAILURE" in payload["messages"][0]["content"]:
        raise urllib.error.HTTPError(request.full_url, 503, "synthetic", {}, None)
    return FakeResponse()


urllib.request.OpenerDirector.open = fake_open
app = create_app(settings, experiment_owner_verifier=fake_verifier)


@asynccontextmanager
async def no_database_lifespan(app):
    yield


app.router.lifespan_context = no_database_lifespan


@app.get("/fixture-page")
def fixture_page(request: Request):
    return render_page(request, "404.html", {"title": "合成主站页"})


@app.get("/synthetic-call-count")
def synthetic_call_count():
    return {"calls": len(calls)}
