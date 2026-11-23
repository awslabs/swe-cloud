"""The API keeps working: push, pull, list, delete, re-push, data written by earlier versions."""

from harness import check_end_state, manifest


def test_push_pull_and_list_across_replicas(cluster):
    repo = cluster.new_repo("api")
    m1, d1 = manifest(repo + ":1")
    m2, d2 = manifest(repo + ":2")
    m3, d3 = manifest(repo + ":3")

    r = cluster.b.push(repo, "v1", m1)
    assert r.status == 201, r
    assert r.headers.get("Docker-Content-Digest") == d1
    assert cluster.a.push(repo, "v2", m2).status == 201

    g = cluster.a.get(repo, "v1")
    assert g.status == 200 and g.body == m1, g
    g = cluster.b.get(repo, d2)
    assert g.status == 200 and g.body == m2, g
    for rep in (cluster.a, cluster.b):
        r = rep.tags_list(repo)
        assert r.status == 200 and sorted(r.json()["tags"]) == ["v1", "v2"], r

    assert cluster.b.push(repo, "v1", m2).status == 201
    g = cluster.a.get(repo, "v1")
    assert g.status == 200 and g.body == m2, g

    assert cluster.a.push(repo, d3, m3).status == 201
    g = cluster.b.get(repo, d3)
    assert g.status == 200 and g.body == m3, g
    assert cluster.audit.live(repo, d3)
    assert sorted(cluster.b.tags_list(repo).json()["tags"]) == ["v1", "v2"]


def test_delete_unknown_digest_is_404(cluster):
    repo, _, _, _ = cluster.seed_case("unknown", 2)
    _, never = manifest(repo + ":never")
    for rep in (cluster.a, cluster.b):
        r = rep.delete(repo, never)
        assert r.status == 404, r
        assert b"MANIFEST_UNKNOWN" in r.body, r
    r = cluster.a.delete(cluster.new_repo("empty"), never)
    assert r.status == 404, r


def test_delete_then_push_again(cluster):
    repo = cluster.new_repo("again")
    m, d = manifest(repo + ":D")
    mx, x = manifest(repo + ":X")
    assert cluster.b.push(repo, "v1", m).status == 201
    assert cluster.b.push(repo, "v2", m).status == 201
    assert cluster.a.push(repo, "other", mx).status == 201

    r = cluster.a.delete(repo, d)
    assert r.status == 202, r
    assert cluster.a.get(repo, d).status == 404
    assert cluster.b.get(repo, "v1").status == 404
    for rep in (cluster.a, cluster.b):
        assert sorted(rep.tags_list(repo).json()["tags"]) == ["other"]
    assert not cluster.audit.live(repo, d)
    assert cluster.audit.broken(repo) == []
    r = cluster.b.delete(repo, d)
    assert r.status == 404, r

    assert cluster.b.push(repo, "again", m).status == 201
    g = cluster.a.get(repo, "again")
    assert g.status == 200 and g.body == m, g
    assert cluster.a.get(repo, d).status == 200
    assert cluster.audit.live(repo, d)
    for rep in (cluster.a, cluster.b):
        assert sorted(rep.tags_list(repo).json()["tags"]) == ["again", "other"]
    assert cluster.b.get(repo, "v1").status == 404

    r = cluster.b.delete(repo, d)
    assert r.status == 202, r
    assert cluster.audit.resolving(repo) == ["other"]


def test_existing_data_pull_and_delete(cluster):
    repo, (m, d), (mx, x), seeded = cluster.seed_case("legacy", 5)
    for rep in (cluster.a, cluster.b):
        g = rep.get(repo, "t003")
        assert g.status == 200 and g.body == m, g
        r = rep.tags_list(repo)
        assert r.status == 200 and sorted(r.json()["tags"]) == sorted(seeded + ["other"]), r
    r = cluster.b.delete(repo, d)
    assert r.status == 202, r
    problems = check_end_state(cluster, repo, d, seeded, "other", x, attempts=[r])
    assert not problems, "\n".join(problems)
