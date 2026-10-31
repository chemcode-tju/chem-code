"""Project persistence, non-destructive folder changes and account boundaries."""

import httpx
from fastapi.testclient import TestClient

from chemistry_agent.api import create_app
from chemistry_agent.auth import create_user_account


def test_project_lifecycle_survives_restart_and_preserves_chat(settings_factory):
    settings = settings_factory()
    with TestClient(create_app(settings)) as client:
        session = client.post("/sessions", json={"title": "CO adsorption"}).json()
        project = client.post("/api/projects", json={"name": "  Catalysis  "}).json()
        other = client.post("/api/projects", json={"name": "Surfaces"}).json()
        route = f"/api/projects/sessions/{session['id']}"
        assert project["name"] == "Catalysis"
        assert client.put(route, json={"project_id": project["id"]}).status_code == 204
        assert (
            client.patch(
                f"/api/projects/{project['id']}", json={"name": "CO reduction"}
            ).status_code
            == 200
        )
        assert client.post("/api/projects", json={"name": "   "}).status_code == 422
        assert client.post("/api/projects", json={"name": "x" * 81}).status_code == 422
    with TestClient(create_app(settings)) as client:
        projects = client.get("/api/projects").json()
        assert projects[0]["name"] == "CO reduction"
        assert projects[0]["sessions"][0]["id"] == session["id"]
        assert client.put(route, json={"project_id": other["id"]}).status_code == 204
        projects = client.get("/api/projects").json()
        assert not projects[0]["sessions"]
        assert len(projects[1]["sessions"]) == 1
        assert client.delete(f"/api/projects/{other['id']}").status_code == 204
        assert client.get(f"/sessions/{session['id']}").status_code == 200
        assert client.put(route, json={"project_id": None}).status_code == 204
        assert all(not p["sessions"] for p in client.get("/api/projects").json())


async def test_project_membership_cannot_cross_account_boundaries(settings_factory):
    settings = settings_factory(auth_enabled=True, cookie_secure=False)
    password = "project-test-password"
    await create_user_account(settings, "alice", password)
    await create_user_account(settings, "bob", password)
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as alice,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as bob,
    ):
        assert (await bob.get("/api/projects")).status_code == 401
        for client, name in [(alice, "alice"), (bob, "bob")]:
            await client.post("/auth/login", json={"username": name, "password": password})
        project = (await alice.post("/api/projects", json={"name": "Private"})).json()
        session = (await alice.post("/sessions", json={"title": "Private chat"})).json()
        bob_session = (await bob.post("/sessions", json={"title": "Bob chat"})).json()
        project_route = f"/api/projects/{project['id']}"
        session_route = f"/api/projects/sessions/{session['id']}"
        assert (
            await alice.put(session_route, json={"project_id": project["id"]})
        ).status_code == 204
        assert (await bob.get("/api/projects")).json() == []
        assert (await bob.patch(project_route, json={"name": "Hijacked"})).status_code == 404
        assert (await bob.delete(project_route)).status_code == 404
        assert (await bob.put(session_route, json={"project_id": None})).status_code == 404
        assert (
            await bob.put(
                f"/api/projects/sessions/{bob_session['id']}", json={"project_id": project["id"]}
            )
        ).status_code == 404
        assert (
            await alice.put(
                session_route, json={"project_id": None}, headers={"Origin": "https://evil.test"}
            )
        ).status_code == 403
        assert (await alice.get("/api/projects")).json()[0]["sessions"][0]["id"] == session["id"]
