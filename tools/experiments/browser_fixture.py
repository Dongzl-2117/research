"""仅供浏览器验收：临时合成快照、SQLite 和无网络模型替身。"""
import os
from pathlib import Path

from researchlib.experiments.demo import demo_snapshot
from researchlib.experiments.store import Store
from researchlib.web.experiments import create_experiment_app

snapshot = demo_snapshot()
# 故意包含 HTML，验收页面只按原文显示，不执行原文/回答里的标签。
snapshot.cases[0].rows[0].body += '<img src=x onerror="window.compromised=true">'
path = Path(os.environ["EXPERIMENT_SMOKE_DIR"]) / "synthetic.sqlite3"
Store.initialize(path, snapshot.fingerprint)


def fake_generator(*, messages, max_output_tokens):
    return {
        "text": '合成模型替身：会员费支持低价零售。[1]\n<script>window.compromised=true</script>',
        "model": "synthetic-no-network", "model_version": "browser-v1",
        "input_tokens": 100, "output_tokens": 25,
    }


app = create_experiment_app(snapshot, path, generator=fake_generator, generator_label="synthetic-no-network:browser-v1")
