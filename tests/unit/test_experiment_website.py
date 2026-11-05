"""主站挂载、服务端身份、来源、会话 CSRF 和显式费用确认；所有模型/认证均为离线替身。"""
from __future__ import annotations

import io
import json
import re
import urllib.error
import urllib.request
from dataclasses import asdict

import pytest
import yaml
from fastapi import Request
from fastapi.testclient import TestClient

from researchlib import llm
from researchlib.config import Settings
from researchlib.experiments.demo import demo_snapshot
from researchlib.experiments.schema import Snapshot
from researchlib.experiments.site_model import MAX_RESPONSE_BYTES, SiteModelGenerator, _NoRedirect
from researchlib.experiments.store import Store
from researchlib.web.app import create_app
from researchlib.web.experiment_access import ADMIN_PAGE, ExperimentOwner
from researchlib.web.routes import render_page

BASE = "/admin/algorithms/api/experiments/"
ORIGIN = "https://library.example"


@pytest.fixture(autouse=True)
def no_real_model_or_credentials(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("测试禁止真实网络或凭据访问")
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", forbidden)
    monkeypatch.setattr(llm, "_deepseek_token", lambda: "synthetic-token-only")
    monkeypatch.setattr(llm, "_oauth_token", lambda *args, **kwargs: "synthetic-oauth-only")


def setup_site(tmp_path, *, models=False, provider="deepseek", private=False, daily=5):
    snapshot = demo_snapshot()
    if private:
        snapshot = snapshot.model_copy(update={"material": "private_corpus"})
    path, db, sources = tmp_path / "snapshot.json", tmp_path / "comparison.sqlite3", tmp_path / "sources.yaml"
    path.write_text(snapshot.model_dump_json())
    sources.write_text(yaml.safe_dump({"sources": [asdict(s) for s in snapshot.sources]}, allow_unicode=True))
    settings = Settings.from_env({
        "DATA_DIR": str(tmp_path / "data"), "PUBLIC_BASE_URL": ORIGIN, "SOURCES_FILE": str(sources),
        "RESEARCH_EXPERIMENTS_ENABLED": "1", "RESEARCH_EXPERIMENTS_SNAPSHOT": str(path),
        "RESEARCH_EXPERIMENTS_DB": str(db), "RESEARCH_EXPERIMENTS_SITE_MODEL": "1" if models else "0",
        "RESEARCH_EXPERIMENTS_DAILY_PAIRS": str(daily), "RESEARCH_LLM_PROVIDER": provider,
    })
    store = Store.initialize(db, snapshot.fingerprint)
    sessions = {
        "owner-one": ExperimentOwner("synthetic-owner", "session-one", frozenset(s.id for s in snapshot.sources)),
        "owner-two": ExperimentOwner("synthetic-owner", "session-two", frozenset(s.id for s in snapshot.sources)),
        "limited": ExperimentOwner("limited-owner", "limited-session", frozenset({"demo-a"})),
    }
    async def verifier(request):
        # 仅测试替身；生产没有此头认证，必须接上真正可核验的会话/签名验证。
        return sessions.get(request.headers.get("x-test-session"))
    app = create_app(settings, experiment_owner_verifier=verifier)
    @app.get("/fixture-page")
    def fixture_page(request: Request):
        return render_page(request, "404.html", {"title": "合成导航页"})
    client = TestClient(app, base_url=ORIGIN, client=("192.0.2.10", 5000))
    client.headers.update({"x-test-session": "owner-one", "origin": ORIGIN})
    page = client.get(ADMIN_PAGE)
    assert page.status_code == 200, page.text
    client.headers["x-experiment-csrf"] = re.search(r'name="experiment-csrf" content="([^"]+)"', page.text)[1]
    return settings, snapshot, store, sessions, client


def start(client):
    response = client.post(BASE + "compare", json={
        "case_ids": ["demo-1", "demo-2"], "candidate": {"prompt_note": "先列分歧"},
    })
    assert response.status_code == 200, response.text
    return response.json()


def generate(client, run, *, case="demo-1", **extra):
    state = client.get(BASE + "state").json()
    return client.post(BASE + f"runs/{run['id']}/generate", json={
        "case_id": case, "acknowledge_cost": True, "request_id": "synthetic-request-" + case,
        "configuration_id": (state["generation_plan"] or {}).get("id"), **extra,
    })


def answer():
    return {"model": "synthetic-response-model", "choices": [{"finish_reason": "stop", "message": {
        "content": "合成回答仅依原文 [1]", "reasoning_content": "合成思考，不展示",
    }}], "usage": {"prompt_tokens": 200, "completion_tokens": 30}}


class Response:
    def __init__(self, body):
        self.raw = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self, n):
        return self.raw[:n]


def test_default_closed_and_flag_without_auth_cannot_load_private_files(tmp_path):
    settings = Settings.from_env({"DATA_DIR": str(tmp_path)})
    assert not settings.experiments_enabled and not settings.experiments_site_model
    app = create_app(settings)
    client = TestClient(app)
    assert client.get(ADMIN_PAGE).status_code == 404
    assert client.get(BASE + "state").status_code == 404
    enabled = Settings.from_env({"RESEARCH_EXPERIMENTS_ENABLED": "1"})
    with pytest.raises(ValueError, match="所有者认证"):
        create_app(enabled)


def test_owner_navigation_mounted_urls_and_default_no_model(tmp_path):
    _, _, _, _, client = setup_site(tmp_path)
    page = client.get(ADMIN_PAGE)
    assert '/admin/algorithms/api/experiments/' in page.text
    assert '/admin/algorithms/experiments/static/experiments.js' in page.text
    assert '所有者管理' in page.text
    assert client.get('/admin/algorithms/experiments/static/experiments.js').status_code == 200
    assert ADMIN_PAGE in client.get('/fixture-page').text
    state = client.get(BASE + 'state').json()
    assert state['access_mode'] == 'owner_website' and state['generator'] is None
    run = start(client)
    assert generate(client, run).status_code == 503
    client.headers.pop('x-test-session')
    assert ADMIN_PAGE not in client.get('/fixture-page').text


@pytest.mark.parametrize('headers', [
    {}, {'x-test-session': 'limited'}, {'x-owner': 'true'},
    {'cf-access-authenticated-user-email': 'synthetic-owner@example.test'},
    {'x-forwarded-for': '127.0.0.1', 'x-real-ip': '127.0.0.1'},
])
def test_unauthenticated_or_wrong_source_owner_cannot_read_any_admin_data(tmp_path, headers):
    _, _, _, _, client = setup_site(tmp_path)
    run = start(client)
    client.headers.pop('x-test-session')
    client.headers.update(headers)
    for path in ['state', 'snapshot', f"runs/{run['id']}/export", 'source/demo-1/1/0']:
        assert client.get(BASE + path).status_code == 403
    assert client.get(ADMIN_PAGE).status_code == 403
    assert client.post(BASE + 'compare', json={'case_ids': ['demo-1']}).status_code == 403


def test_csrf_binds_real_session_and_rejects_host_origin_and_credential_revocation(tmp_path):
    _, _, store, sessions, client = setup_site(tmp_path)
    run = start(client)
    client.headers['x-test-session'] = 'owner-two'
    assert client.post(BASE + 'compare', json=run['request']).status_code == 403
    page = client.get(ADMIN_PAGE)
    client.headers['x-experiment-csrf'] = re.search(r'name="experiment-csrf" content="([^"]+)"', page.text)[1]
    assert client.post(BASE + 'compare', json=run['request']).status_code == 200
    for headers in (
        {'origin': 'https://untrusted.example'}, {'host': 'untrusted.example'}, {'sec-fetch-site': 'cross-site'},
    ):
        assert client.get(BASE + 'state', headers=headers).status_code == 403
        assert client.post(BASE + 'compare', json=run['request'], headers=headers).status_code == 403
    sessions.pop('owner-two')
    assert client.get(BASE + f"runs/{run['id']}/export").status_code == 403
    assert len(store.history()) == 1


@pytest.mark.parametrize('provider', ['deepseek', 'grok'])
def test_supported_site_model_only_after_confirmed_plan_and_two_bounded_requests(tmp_path, monkeypatch, provider):
    calls = []
    def fake_open(self, req, timeout=None):
        calls.append((json.loads(req.data), timeout, req.full_url))
        return Response(answer())
    monkeypatch.setattr(urllib.request.OpenerDirector, 'open', fake_open)
    _, _, store, _, client = setup_site(tmp_path, models=True, provider=provider)
    state = client.get(BASE + 'state').json()
    assert state['generation_plan']['max_model_requests'] == 2 and state['generation_plan']['cost'] is None
    assert calls == []
    run = start(client)
    assert calls == []
    assert generate(client, run, acknowledge_cost=False).status_code == 422
    assert generate(client, run, configuration_id='stale-plan').status_code == 409
    assert calls == []
    response = generate(client, run)
    assert response.status_code == 200 and response.json()['status'] == 'done'
    assert len(calls) == 2
    assert all(payload['max_tokens'] == 1500 and payload['stream'] is False and timeout <= 60
               for payload, timeout, _ in calls)
    assert calls[0][0]['messages'] != calls[1][0]['messages']
    assert '先列分歧' not in calls[0][0]['messages'][0]['content']
    assert '先列分歧' in calls[1][0]['messages'][0]['content']
    data = response.json()
    assert data['actor'] == 'synthetic-owner'
    assert data['sides']['baseline']['model_version'] == 'unknown'
    assert data['sides']['baseline']['cost'] is None
    assert data['sides']['baseline']['input_tokens'] == 200
    assert data['sides']['baseline']['output_tokens'] == 30
    assert '合成思考' not in data['sides']['baseline']['text']
    assert generate(client, run).json() == data and len(calls) == 2
    assert store.daily_generation_count() == 1


@pytest.mark.parametrize('error', [
    'timeout', '429', '500', '401', 'bad-json', 'empty', 'length', 'no-finish', 'error-body', 'oversize', 'over-budget',
])
def test_bounded_model_errors_never_retry_or_increase_budget(tmp_path, monkeypatch, error):
    calls = []
    def fake_open(self, req, timeout=None):
        calls.append(json.loads(req.data))
        if error == 'timeout':
            raise TimeoutError('synthetic-secret-do-not-return')
        if error in {'429','500','401'}:
            raise urllib.error.HTTPError(req.full_url, int(error), 'synthetic-error', {}, io.BytesIO(b'private-detail'))
        body = answer()
        if error == 'bad-json':
            return Response(b'not-json')
        if error == 'empty':
            body['choices'][0]['message']['content'] = ''
        if error in {'length','no-finish'}:
            body['choices'][0]['finish_reason'] = 'length' if error == 'length' else None
        if error == 'error-body':
            body['error'] = {'message': 'private-detail'}
        if error == 'oversize':
            return Response(b'x' * (MAX_RESPONSE_BYTES + 1))
        if error == 'over-budget':
            body['usage']['completion_tokens'] = 1501
        return Response(body)
    monkeypatch.setattr(urllib.request.OpenerDirector, 'open', fake_open)
    _, _, store, _, client = setup_site(tmp_path, models=True)
    run = start(client)
    response = generate(client, run)
    assert response.json()['status'] == 'failed' and len(calls) == 1
    assert '可能已计费' in response.json()['error']
    assert 'private-detail' not in response.text and 'synthetic-secret' not in response.text
    assert generate(client, run).json() == response.json() and len(calls) == 1
    assert calls[0]['max_tokens'] == 1500 and store.daily_generation_count() == 1


def test_daily_quota_counts_failed_or_unknown_claims_and_does_not_call_model(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        urllib.request.OpenerDirector, 'open', lambda *args, **kwargs: calls.append(1) or Response(answer()),
    )
    _, _, store, _, client = setup_site(tmp_path, models=True, daily=1)
    run = start(client)
    store.claim_generation(run['id'], 'demo-1', 'unknown-outcome', 'synthetic-model', daily_limit=1)
    response = generate(client, run, case='demo-2')
    assert response.status_code == 409 and calls == []
    assert '额度' in response.text and client.get(BASE + 'state').json()['daily_used'] == 1


@pytest.mark.parametrize('revoke', ['source', 'session'])
@pytest.mark.parametrize('revoke_on', [1, 2])
def test_permissions_revoked_between_model_sides_stop_second_and_hide_partial_content(
    tmp_path, monkeypatch, revoke, revoke_on,
):
    settings, _, store, sessions, client = setup_site(tmp_path, models=True, private=True)
    calls = []
    def fake_open(self, req, timeout=None):
        calls.append(1)
        if len(calls) != revoke_on:
            return Response(answer())
        if revoke == 'session':
            sessions.clear()
        else:
            payload = yaml.safe_load(settings.sources_path.read_text())
            payload['sources'][0]['enabled'] = False
            settings.sources_path.write_text(yaml.safe_dump(payload))
        return Response(answer())
    monkeypatch.setattr(urllib.request.OpenerDirector, 'open', fake_open)
    run = start(client)
    response = generate(client, run)
    assert response.status_code == 403 and len(calls) == revoke_on
    assert '合成回答' not in response.text
    assert store.generation(run['id'], 'demo-1')['status'] == 'failed'
    assert client.get(BASE + f"runs/{run['id']}/export").status_code == 403


def test_no_redirect_of_private_material_or_token():
    handler = _NoRedirect()
    assert handler.redirect_request(None, None, 302, 'redirect', {}, 'https://other.example') is None


def test_site_model_preparation_does_not_read_credentials_and_disabled_switch_does_not_enable(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('不应读凭据或调用模型')
    monkeypatch.setattr(llm, '_deepseek_token', forbidden)
    monkeypatch.setattr(llm, '_oauth_token', forbidden)
    settings = Settings.from_env({'DATA_DIR': str(tmp_path)})
    generator = SiteModelGenerator(settings)
    assert generator.plan['max_output_tokens_per_side'] == 1500
    assert Settings.from_env({'RESEARCH_EXPERIMENTS_SITE_MODEL':'1'}).experiments_enabled is False


def test_unverifiable_principal_or_verifier_error_fail_closed(tmp_path):
    settings, snapshot, _, _, _ = setup_site(tmp_path)
    for outcome in ({'owner': True}, True, ExperimentOwner('x', 's', frozenset()), None):
        app = create_app(settings, experiment_owner_verifier=lambda request, value=outcome: value)
        client = TestClient(app, base_url=ORIGIN)
        assert client.get(BASE + 'state').status_code == 403
    def broken(request):
        raise RuntimeError('synthetic-credential-detail')
    client = TestClient(create_app(settings, experiment_owner_verifier=broken), base_url=ORIGIN)
    response = client.get(BASE + 'state')
    assert response.status_code == 403 and 'synthetic-credential-detail' not in response.text
    assert Snapshot.model_validate_json(settings.experiments_snapshot.read_text()).fingerprint == snapshot.fingerprint


def test_implicit_and_explicit_default_parameters_reuse_same_run(tmp_path):
    _, _, store, _, client = setup_site(tmp_path)
    run = start(client)
    replay = client.post(BASE + "compare", json=run["request"])
    assert replay.status_code == 200 and replay.json() == run
    assert len(store.history()) == 1


def test_concurrent_cases_cannot_exceed_daily_model_quota(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    begun, finish = Event(), Event()
    calls = []
    def fake_open(self, req, timeout=None):
        calls.append(1)
        begun.set()
        assert finish.wait(5)
        return Response(answer())
    monkeypatch.setattr(urllib.request.OpenerDirector, 'open', fake_open)
    _, _, store, _, client = setup_site(tmp_path, models=True, daily=1)
    run = start(client)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(generate, client, run)
        assert begun.wait(5)
        rejected = generate(client, run, case='demo-2')
        assert rejected.status_code == 409
        finish.set()
        assert pending.result().json()['status'] == 'done'
    assert calls == [1, 1] and store.daily_generation_count() == 1


def test_missing_site_credentials_fail_without_network_or_retry(tmp_path, monkeypatch):
    calls = []
    def unavailable():
        calls.append(1)
        raise llm.LLMUnavailable('synthetic-credential-error')
    monkeypatch.setattr(llm, '_deepseek_token', unavailable)
    _, _, _, _, client = setup_site(tmp_path, models=True)
    run = start(client)
    response = generate(client, run)
    assert response.json()['status'] == 'failed' and calls == [1]
    assert 'synthetic-credential-error' not in response.text
    assert generate(client, run).json() == response.json() and calls == [1]


def test_changed_site_model_plan_requires_new_comparison_before_model_call(tmp_path):
    from dataclasses import replace
    settings, _, store, sessions, first = setup_site(tmp_path, models=True)
    old_run = start(first)
    new_app = create_app(
        replace(settings, llm_provider="grok", llm_base_url="https://api.x.ai/v1"),
        experiment_owner_verifier=lambda request: sessions.get(request.headers.get("x-test-session")),
    )
    second = TestClient(new_app, base_url=ORIGIN)
    second.headers.update({"x-test-session": "owner-one", "origin": ORIGIN})
    page = second.get(ADMIN_PAGE)
    second.headers["x-experiment-csrf"] = re.search(r'name="experiment-csrf" content="([^"]+)"', page.text)[1]
    assert generate(second, old_run).status_code == 409
    new_run = start(second)
    assert new_run["id"] != old_run["id"]
    assert new_run["generation_profile"] != old_run["generation_profile"]
    assert len(store.history()) == 2 and store.get(old_run["id"]) == old_run


@pytest.mark.parametrize('provider', ['deepseek', 'grok'])
@pytest.mark.parametrize('revoke', ['source', 'session'])
def test_revoked_while_waiting_for_model_slot_never_reads_credentials_or_posts(
    tmp_path, monkeypatch, provider, revoke,
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import BoundedSemaphore, Event

    settings, _, store, sessions, client = setup_site(tmp_path, models=True, provider=provider, private=True)
    run = start(client)
    slot, waiting = BoundedSemaphore(1), Event()
    posts, credentials = [], []

    class QueuedSlot:
        def __enter__(self):
            waiting.set()
            assert slot.acquire(timeout=5)
            return self
        def __exit__(self, *args):
            slot.release()

    monkeypatch.setattr(llm, '_slot', lambda settings: QueuedSlot())
    monkeypatch.setattr(llm, '_deepseek_token', lambda: credentials.append('deepseek') or 'synthetic-token')
    monkeypatch.setattr(llm, '_oauth_token', lambda *args, **kwargs: credentials.append('grok') or 'synthetic-token')
    def fake_open(self, req, timeout=None):
        posts.append(req.full_url)
        return Response(answer())
    monkeypatch.setattr(urllib.request.OpenerDirector, 'open', fake_open)

    assert slot.acquire(timeout=5)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(generate, client, run)
        try:
            assert waiting.wait(5) and not pending.done()
            if revoke == 'session':
                sessions.clear()
            else:
                sources = yaml.safe_load(settings.sources_path.read_text())
                sources['sources'][0]['enabled'] = False
                settings.sources_path.write_text(yaml.safe_dump(sources))
        finally:
            slot.release()
        response = pending.result(timeout=5)

    assert response.status_code == 403
    assert posts == [] and credentials == []
    assert '合成回答' not in response.text
    record = store.generation(run['id'], 'demo-1')
    assert record['status'] == 'failed' and record['sides'] == {}
    # 占位/适配器尝试包含排队，不等于已经发送了模型 POST；仍保守占用每日额度。
    assert record['attempted_sides'] == ['baseline'] and store.daily_generation_count() == 1
    assert client.get(BASE + f"runs/{run['id']}/export").status_code == 403


@pytest.mark.parametrize('revoke', ['source', 'session'])
def test_permission_revoked_during_credential_preparation_stops_model_post(tmp_path, monkeypatch, revoke):
    settings, _, store, sessions, client = setup_site(tmp_path, models=True, provider='grok', private=True)
    run = start(client)
    posts = []
    def prepare_credentials(*args, **kwargs):
        # 模拟 OAuth 凭据准备期间发生撤销；不读真实凭据，也不发送认证或模型请求。
        if revoke == 'session':
            sessions.clear()
        else:
            sources = yaml.safe_load(settings.sources_path.read_text())
            sources['sources'][0]['enabled'] = False
            settings.sources_path.write_text(yaml.safe_dump(sources))
        return 'synthetic-token-only'
    monkeypatch.setattr(llm, '_oauth_token', prepare_credentials)
    def fake_open(self, req, timeout=None):
        posts.append(req.full_url)
        return Response(answer())
    monkeypatch.setattr(urllib.request.OpenerDirector, 'open', fake_open)
    response = generate(client, run)
    assert response.status_code == 403 and posts == []
    assert store.generation(run['id'], 'demo-1')['status'] == 'failed'


def test_site_model_requires_a_send_authorization_callback_before_credentials(tmp_path, monkeypatch):
    credentials = []
    monkeypatch.setattr(llm, '_deepseek_token', lambda: credentials.append(1) or 'synthetic-token')
    generator = SiteModelGenerator(Settings.from_env({'DATA_DIR': str(tmp_path)}))
    with pytest.raises(ValueError, match='授权检查'):
        generator(messages=[{'role': 'user', 'content': '合成问题'}], max_output_tokens=1500, before_send=None)
    assert credentials == []
