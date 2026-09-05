"""Canvas Phase 2 — canvas_edge table, edge CRUD, validation, cascades."""
from __future__ import annotations

import itertools

import calliope.config as config_module
from calliope.db import get_db


def _mk_project(client) -> int:
    return client.post("/api/projects", json={"title": "E", "idea": "x"}).json()["id"]


def _mk_canvas(client, pid: int, *, seed: bool = False) -> int:
    if seed:
        _seed_entities(client, pid)
    r = client.post("/api/canvas", json={"project_id": pid})
    assert r.status_code == 200, r.text
    return r.json()["canvas"]["id"]


def _mk_workflow(client) -> int:
    # Unique class name per call: byte-identical re-imports are rejected with
    # 409 by the dedupe guard, so fixtures must not collide.
    n = next(_wf_counter)
    r = client.post(
        "/api/workflows",
        json={
            "name": "img2vid",
            "kind": "video",
            "workflow_json": {"9": {"class_type": f"Loading{n}", "inputs": {}}},
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


_wf_counter = itertools.count()


def _seed_entities(client, pid: int) -> None:
    conn = get_db(config_module.settings.db_path)
    try:
        conn.execute(
            "INSERT INTO characters (project_id, name, appearance) VALUES (?, ?, ?)",
            (pid, "Kira", "fighter"),
        )
        conn.execute(
            "INSERT INTO characters (project_id, name, appearance) VALUES (?, ?, ?)",
            (pid, "Yun", "rival"),
        )
        conn.commit()
    finally:
        conn.close()


def _entity_nodes(client, canvas_id: int) -> list[dict]:
    graph = client.get(f"/api/canvas/{canvas_id}").json()
    return [n for n in graph["nodes"] if n["type"] == "entity"]


def _mk_node(client, canvas_id: int, **kw) -> int:
    if kw.get("type") == "workflow" and "workflow_id" not in kw:
        kw["workflow_id"] = _mk_workflow(client)
    r = client.post(f"/api/canvas/{canvas_id}/nodes", json=kw)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _mk_edge(client, canvas_id: int, **payload):
    return client.post(f"/api/canvas/{canvas_id}/edges", json=payload)


def test_canvas_edge_table_exists(client):
    conn = get_db(config_module.settings.db_path)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(canvas_edge)").fetchall()}
        assert {
            "id",
            "canvas_id",
            "src_node_id",
            "dst_node_id",
            "kind",
            "label",
            "dst_role",
            "dst_comfy_node_id",
        } <= cols
    finally:
        conn.close()


def test_node_creation_endpoint(client):
    pid = _mk_project(client)
    cid = _mk_canvas(client, pid)
    wid = _mk_workflow(client)
    r = client.post(
        f"/api/canvas/{cid}/nodes",
        json={"type": "workflow", "workflow_id": wid, "title": "H3 node", "x": 900, "y": 120},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["type"] == "workflow"
    assert body["workflow_id"] == wid
    assert body["input_values_json"] == "{}"

    r = client.post(
        f"/api/canvas/{cid}/nodes",
        json={"type": "image", "artifact_path": "C:/assets/out.png", "title": "gen"},
    )
    assert r.status_code == 200
    assert r.json()["artifact_path"] == "C:/assets/out.png"

    # entity nodes come from auto-seed only; unknown types rejected
    assert client.post(f"/api/canvas/{cid}/nodes", json={"type": "entity"}).status_code == 422
    assert client.post(f"/api/canvas/{cid}/nodes", json={"type": "bogus"}).status_code == 422
    assert (
        client.post(f"/api/canvas/{cid}/nodes", json={"type": "workflow"}).status_code == 422
    ), "workflow nodes require workflow_id"


def test_create_link_edge_and_roundtrip(client):
    pid = _mk_project(client)
    cid = _mk_canvas(client, pid, seed=True)
    nodes = _entity_nodes(client, cid)
    a, b = nodes[0]["id"], nodes[1]["id"]
    r = _mk_edge(client, cid, src_node_id=a, dst_node_id=b, kind="link", label="rival of")
    assert r.status_code == 200, r.text

    graph = client.get(f"/api/canvas/{cid}").json()
    assert len(graph["edges"]) == 1
    assert graph["edges"][0]["label"] == "rival of"
    assert graph["edges"][0]["kind"] == "link"


def test_data_edge_requires_dst_role(client):
    pid = _mk_project(client)
    cid = _mk_canvas(client, pid)
    a = _mk_node(client, cid, type="image", artifact_path="C:/a/x.png")
    b = _mk_node(client, cid, type="workflow")
    r = _mk_edge(client, cid, src_node_id=a, dst_node_id=b, kind="data")
    assert r.status_code == 422
    r = _mk_edge(
        client,
        cid,
        src_node_id=a,
        dst_node_id=b,
        kind="data",
        dst_role="image",
        dst_comfy_node_id="12",
    )
    assert r.status_code == 200, r.text


def test_duplicate_edge_pair_rejected(client):
    pid = _mk_project(client)
    cid = _mk_canvas(client, pid, seed=True)
    nodes = _entity_nodes(client, cid)
    a, b = nodes[0]["id"], nodes[1]["id"]
    assert (
        _mk_edge(client, cid, src_node_id=a, dst_node_id=b, kind="link", label="x").status_code
        == 200
    )
    assert (
        _mk_edge(client, cid, src_node_id=a, dst_node_id=b, kind="link", label="y").status_code
        == 422
    )
    # different kind on same pair is fine
    assert (
        _mk_edge(
            client,
            cid,
            src_node_id=a,
            dst_node_id=b,
            kind="data",
            dst_role="image",
        ).status_code
        == 200
    )


def test_cross_canvas_edge_rejected(client):
    pid = _mk_project(client)
    cid = _mk_canvas(client, pid, seed=True)
    other = _mk_canvas(client, _mk_project(client))
    a = _entity_nodes(client, cid)[0]["id"]
    b = _mk_node(client, other, type="image", artifact_path="C:/o/x.png")
    r = _mk_edge(client, cid, src_node_id=a, dst_node_id=b, kind="link", label="x")
    assert r.status_code in (404, 422)


def test_data_edge_cycle_rejected(client):
    pid = _mk_project(client)
    cid = _mk_canvas(client, pid)
    # chain of artifact nodes — sources must have an image/artifact
    a = _mk_node(client, cid, type="image", artifact_path="C:/a/1.png")
    b = _mk_node(client, cid, type="image", artifact_path="C:/a/2.png")
    c = _mk_node(client, cid, type="image", artifact_path="C:/a/3.png")
    wf = _mk_node(client, cid, type="workflow")
    ok1 = _mk_edge(client, cid, src_node_id=a, dst_node_id=b, kind="data", dst_role="image")
    ok2 = _mk_edge(
        client,
        cid,
        src_node_id=b,
        dst_node_id=c,
        kind="data",
        dst_role="image",
    )
    ok3 = _mk_edge(client, cid, src_node_id=a, dst_node_id=wf, kind="data", dst_role="prompt")
    assert (
        ok1.status_code == 200 and ok2.status_code == 200 and ok3.status_code == 200
    ), (ok1.text, ok2.text, ok3.text)
    # c → a closes the cycle — must 422
    r = _mk_edge(client, cid, src_node_id=c, dst_node_id=a, kind="data", dst_role="image")
    assert r.status_code == 422
    # self-loop also rejected
    r = _mk_edge(client, cid, src_node_id=a, dst_node_id=a, kind="data", dst_role="image")
    assert r.status_code == 422


def test_link_edges_may_loop(client):
    pid = _mk_project(client)
    cid = _mk_canvas(client, pid, seed=True)
    nodes = _entity_nodes(client, cid)
    a, b = nodes[0]["id"], nodes[1]["id"]
    assert (
        _mk_edge(client, cid, src_node_id=a, dst_node_id=b, kind="link", label="x").status_code
        == 200
    )
    assert (
        _mk_edge(client, cid, src_node_id=b, dst_node_id=a, kind="link", label="y").status_code
        == 200
    )


def test_link_label_bounds(client):
    pid = _mk_project(client)
    cid = _mk_canvas(client, pid, seed=True)
    nodes = _entity_nodes(client, cid)
    a, b = nodes[0]["id"], nodes[1]["id"]
    assert (
        _mk_edge(client, cid, src_node_id=a, dst_node_id=b, kind="link", label="").status_code
        == 422
    )
    assert (
        _mk_edge(
            client, cid, src_node_id=a, dst_node_id=b, kind="link", label="x" * 81
        ).status_code
        == 422
    )


def test_data_edge_requires_image_source(client):
    pid = _mk_project(client)
    cid = _mk_canvas(client, pid)
    # workflow → workflow: a workflow node is not an image source until it has
    # produced an artifact
    a = _mk_node(client, cid, type="workflow")
    b = _mk_node(client, cid, type="workflow")
    r = _mk_edge(client, cid, src_node_id=a, dst_node_id=b, kind="data", dst_role="image")
    assert r.status_code == 422


def test_patch_edge_label_and_delete(client):
    pid = _mk_project(client)
    cid = _mk_canvas(client, pid, seed=True)
    nodes = _entity_nodes(client, cid)
    a, b = nodes[0]["id"], nodes[1]["id"]
    eid = _mk_edge(client, cid, src_node_id=a, dst_node_id=b, kind="link", label="rival").json()[
        "id"
    ]
    r = client.patch(f"/api/canvas/{cid}/edges/{eid}", json={"label": "mentor of"})
    assert r.status_code == 200
    assert r.json()["label"] == "mentor of"
    assert client.delete(f"/api/canvas/{cid}/edges/{eid}").status_code == 200
    graph = client.get(f"/api/canvas/{cid}").json()
    assert graph["edges"] == []


def test_node_delete_cascades_edges(client):
    pid = _mk_project(client)
    cid = _mk_canvas(client, pid)
    a = _mk_node(client, cid, type="image", artifact_path="C:/a/x.png")
    b = _mk_node(client, cid, type="workflow")
    assert (
        _mk_edge(
            client, cid, src_node_id=a, dst_node_id=b, kind="data", dst_role="image"
        ).status_code
        == 200
    )
    client.delete(f"/api/canvas/{cid}/nodes/{a}")
    graph = client.get(f"/api/canvas/{cid}").json()
    assert graph["edges"] == []


def test_canvas_delete_cascades_edges(client):
    pid = _mk_project(client)
    cid = _mk_canvas(client, pid)
    a = _mk_node(client, cid, type="image", artifact_path="C:/a/x.png")
    b = _mk_node(client, cid, type="workflow")
    _mk_edge(client, cid, src_node_id=a, dst_node_id=b, kind="data", dst_role="image")
    client.delete(f"/api/canvas/{cid}")
    conn = get_db(config_module.settings.db_path)
    try:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM canvas_edge WHERE canvas_id = ?", (cid,)
        ).fetchone()["c"]
        assert n == 0
    finally:
        conn.close()

