"""Web UI: pages render, writes are CSRF-guarded, background jobs complete."""

from __future__ import annotations

import asyncio
import json
import re
import time

import pytest

pytest.importorskip("fastapi")

from fakes import FakeEmbedder, FakeSource, make_work  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from test_knowledge import ScriptedLLM, seed  # noqa: E402

from echo_voc import sources, workspace  # noqa: E402
from echo_voc.label import Labeler  # noqa: E402
from echo_voc.store import db  # noqa: E402
from echo_voc.store.query import FeedbackFilter  # noqa: E402

HX = {"HX-Request": "true"}


@pytest.fixture
def client(monkeypatch, sessionmaker, migrated_db):
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    monkeypatch.setenv("ECHO_MEMORY", "off")
    db.get_sessionmaker.cache_clear()
    from echo_voc.web.app import create_app

    with TestClient(create_app()) as c:
        yield c
    db.get_sessionmaker.cache_clear()


def wait_job(client: TestClient, html: str, timeout: float = 10) -> str:
    job_id = re.search(r'id="job-(\d+)"', html)[1]
    for _ in range(int(timeout * 20)):
        body = client.get(f"/jobs/{job_id}").text
        if 'hx-trigger="every 1s"' not in body:
            return body
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_pages_render_without_data(client):
    for path in ["/", "/accounts", "/analyze", "/ask", "/topics", "/trends", "/settings"]:
        response = client.get(path)
        assert response.status_code == 200, path
        assert "<nav" in response.text


def test_writes_require_htmx_header_and_same_origin(client):
    assert client.post("/accounts", data={"url": "x"}).status_code == 403
    evil = {**HX, "Origin": "https://evil.example"}
    assert client.post("/accounts", data={"url": "x"}, headers=evil).status_code == 403
    assert client.get("/", headers={"Host": "attacker.example"}).status_code == 400


def test_settings_never_leaks_secrets(client, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-super-secret-value")
    monkeypatch.setenv("BILIBILI_COOKIE", "SESSDATA=secret-cookie")
    body = client.get("/settings").text
    assert "sk-super-secret-value" not in body and "secret-cookie" not in body
    assert "已设置" in body


def test_add_account_sync_and_browse(client, monkeypatch):
    fake = FakeSource()
    fake.works = [make_work("v1")]
    from fakes import comment

    fake.comments = {
        "v1": [comment(i, hours_ago=i, text=f"<script>alert({i})</script>") for i in range(3)]
    }
    monkeypatch.setitem(sources.SOURCES, "fake", FakeSource)
    monkeypatch.setattr(FakeSource, "__new__", lambda cls, *a, **k: fake)
    monkeypatch.setattr(FakeSource, "__init__", lambda self, *a, **k: None)

    response = client.post("/accounts", data={"url": "fake://account/acc"}, headers=HX)
    assert response.status_code == 200 and "Fake Studio" in response.text

    job = client.post("/jobs/sync", data={"account": "1"}, headers=HX)
    done = wait_job(client, job.text)
    assert "新增评论 <b>3</b>" in done

    page = client.get("/accounts/1").text
    assert "Work v1" in page
    feedback = client.get("/works/1/feedback").text
    assert "共 3 条" in feedback
    assert "<script>alert" not in feedback and "&lt;script&gt;" in feedback  # escaped


def test_error_goes_to_flash(client):
    response = client.post("/accounts", data={"url": "https://example.com/nothing"}, headers=HX)
    assert response.headers.get("HX-Retarget") == "#flash"
    assert "Could not detect platform" in response.text


def test_ask_topics_and_trend_flow(client, sessionmaker, monkeypatch):
    llm = ScriptedLLM({"intent": "ranking", "search": "top"})
    asyncio.run(
        seed(
            sessionmaker,
            [
                ("crash now", 2),
                ("crash again", 3),
                ("love it", 4),
                ("crash old", 40),
                ("dark please", 41),
            ],
        )
    )
    asyncio.run(Labeler(sessionmaker, llm, FakeEmbedder()).run(FeedbackFilter()))
    monkeypatch.setattr(workspace, "create_llm", lambda **kw: llm)
    monkeypatch.setattr(workspace, "create_embedder", lambda *a, **kw: FakeEmbedder())

    topics = client.get("/topics/table?since=").text
    assert "App crashes on launch" in topics

    answer = wait_job(
        client,
        client.post(
            "/jobs/ask", data={"question": "最近1个月用户反馈较多的问题是什么"}, headers=HX
        ).text,
    )
    assert "App crashes on launch" in answer and 'class="cite"' in answer

    done = wait_job(
        client,
        client.post(
            "/jobs/snapshot", data={"account": "1", "monthly": "3m", "summary": "true"}, headers=HX
        ).text,
    )
    assert "已生成" in done
    snaps = client.get("/trends/snapshots?account=1").text
    labels = re.findall(r'name="labels" value="([^"]+)"', snaps)
    assert len(labels) >= 2
    trend = wait_job(
        client,
        client.post(
            "/jobs/trend",
            data={"account": "1", "labels": labels[-2:], "narrative": "true"},
            headers=HX,
        ).text,
    )
    chart = re.search(r"data-chart='([^']+)'", trend)[1]
    assert json.loads(chart.replace("&#34;", '"'))["series"]
