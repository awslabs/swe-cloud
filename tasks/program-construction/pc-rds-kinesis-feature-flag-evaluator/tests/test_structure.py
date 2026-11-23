"""Variants, segments, constraints, distributions and tags: single operations, their
stored rows, the guards that refuse a change, and the history each change writes."""

import json

from flagr_support import assert_doc, assert_rows

VARIANT_ROWS = "SELECT id, flag_id, key, attachment, deleted_at IS NOT NULL FROM variants ORDER BY id"
SEGMENT_ROWS = "SELECT id, flag_id, description, rank, rollout_percent, deleted_at IS NOT NULL FROM segments ORDER BY id"
CONSTRAINT_ROWS = "SELECT id, segment_id, property, operator, value, deleted_at IS NOT NULL FROM constraints ORDER BY id"
DIST_ROWS = "SELECT id, segment_id, variant_id, variant_key, percent, deleted_at IS NOT NULL FROM distributions ORDER BY id"


def variant_rows(server):
    """Variant rows with the attachment decoded (NULL/empty count as no attachment; json columns are accepted)."""
    out = []
    for vid, fid, key, attachment, deleted in server.rows(VARIANT_ROWS):
        decoded = attachment if isinstance(attachment, dict) else (json.loads(attachment) if attachment else {})
        out.append((vid, fid, key, decoded or {}, deleted))
    return out


def snapshot_count(server, fid):
    return server.rows("SELECT count(*) FROM flag_snapshots WHERE flag_id = %s", (fid,))[0][0]


def two_flags(s):
    s.ok("POST", "/flags", {"description": "f", "key": "f1"})
    s.ok("POST", "/flags", {"description": "g", "key": "g1"})


def test_variants_create_list_and_update(flagr):
    s = flagr(recorder=False)
    two_flags(s)
    attachment = {"hex": "#f00", "n": 1, "nested": {"a": [1, 2.5, True, None], "s": "x"}}
    red = s.ok("POST", "/flags/1/variants", {"key": "red", "attachment": attachment})
    assert_doc(red, {"id": 1, "key": "red", "attachment": attachment})
    assert_doc(s.ok("POST", "/flags/1/variants", {"key": "blue"}), {"id": 2, "key": "blue"})
    # Variant keys are not unique within a flag.
    assert_doc(s.ok("POST", "/flags/1/variants", {"key": "blue"}), {"id": 3, "key": "blue"})
    assert_doc(s.ok("POST", "/flags/2/variants", {"key": "Env:prod/v1.2-x_y"}), {"id": 4, "key": "Env:prod/v1.2-x_y"})
    assert_doc(s.ok("GET", "/flags/1/variants"), [{"id": 1, "key": "red", "attachment": attachment},
                                                  {"id": 2, "key": "blue"}, {"id": 3, "key": "blue"}])
    assert_doc(s.ok("GET", "/flags/2/variants"), [{"id": 4, "key": "Env:prod/v1.2-x_y"}])
    assert_rows(variant_rows(s), [(1, 1, "red", attachment, False), (2, 1, "blue", {}, False),
                                  (3, 1, "blue", {}, False), (4, 2, "Env:prod/v1.2-x_y", {}, False)])
    # PUT replaces the key; the attachment changes only when one is supplied.
    assert_doc(s.ok("PUT", "/flags/1/variants/1", {"key": "crimson"}), {"id": 1, "key": "crimson", "attachment": attachment})
    assert_doc(s.ok("PUT", "/flags/1/variants/1", {"key": "crimson", "attachment": {"hex": "#e00"}}),
               {"id": 1, "key": "crimson", "attachment": {"hex": "#e00"}})
    assert_rows(variant_rows(s)[:1], [(1, 1, "crimson", {"hex": "#e00"}, False)])
    assert snapshot_count(s, 1) == 6 and snapshot_count(s, 2) == 2


def test_variant_rejections_preserve_rows(flagr):
    s = flagr(recorder=False)
    two_flags(s)
    s.ok("POST", "/flags/1/variants", {"key": "red", "attachment": {"a": 1}})
    before = variant_rows(s)
    for method, path, body, status in [
        ("POST", "/flags/1/variants", {"key": "bad key"}, 400),
        ("POST", "/flags/1/variants", {"key": "x", "attachment": [1, 2]}, 400),
        ("POST", "/flags/1/variants", {"key": "x", "attachment": "str"}, 400),
        ("POST", "/flags/1/variants", {"key": ""}, 422),
        ("POST", "/flags/1/variants", {"attachment": {}}, 422),
        ("PUT", "/flags/1/variants/1", {"key": "bad key"}, 400),
        ("PUT", "/flags/1/variants/1", {"key": "x", "attachment": [1]}, 400),
        ("PUT", "/flags/1/variants/99", {"key": "zz"}, 404),
        ("POST", "/flags/999/variants", {"key": "orphan"}, 500),
    ]:
        r = s.call(method, path, body)
        assert r.status == status, (method, path, body, r)
        assert_rows(variant_rows(s), before)
    assert snapshot_count(s, 1) == 2


def test_variant_rename_and_delete_follow_distributions(flagr):
    s = flagr(recorder=False)
    f = s.flag("f1", variants=["red", "blue", "green"])
    seg = s.segment(f, "all", 100, [("red", 60), ("blue", 40)])
    other = s.segment(f, "other", 100, [("blue", 100)])
    # Renaming a variant renames it in every distribution that uses it.
    s.ok("PUT", "/flags/1/variants/2", {"key": "navy"})
    assert_doc(s.ok("GET", f"/flags/1/segments/{seg}/distributions"),
               [{"id": 1, "percent": 60, "variantID": 1, "variantKey": "red"},
                {"id": 2, "percent": 40, "variantID": 2, "variantKey": "navy"}])
    assert_rows(s.rows(DIST_ROWS), [(1, seg, 1, "red", 60, False), (2, seg, 2, "navy", 40, False), (3, other, 2, "navy", 100, False)])
    # A variant with a non-zero share cannot be deleted.
    r = s.call("DELETE", "/flags/1/variants/2")
    assert r.status == 400, r
    assert_rows(s.rows("SELECT id, deleted_at IS NOT NULL FROM variants ORDER BY id"), [(1, False), (2, False), (3, False)])
    # An unused variant can; so can one whose only shares are 0%, which removes those shares.
    assert s.call("DELETE", "/flags/1/variants/3").status == 200
    s.ok("PUT", f"/flags/1/segments/{seg}/distributions", {"distributions": [
        {"variantID": 1, "variantKey": "red", "percent": 100}, {"variantID": 2, "variantKey": "navy", "percent": 0}]})
    s.ok("PUT", f"/flags/1/segments/{other}/distributions", {"distributions": [{"variantID": 1, "variantKey": "red", "percent": 100}]})
    before = snapshot_count(s, 1)
    assert s.call("DELETE", "/flags/1/variants/2").status == 200
    assert_rows(s.rows("SELECT id, deleted_at IS NOT NULL FROM variants ORDER BY id"), [(1, False), (2, True), (3, True)])
    assert_rows(s.rows(DIST_ROWS), [(1, seg, 1, "red", 60, True), (2, seg, 2, "navy", 40, True), (3, other, 2, "navy", 100, True),
                                    (4, seg, 1, "red", 100, False), (5, seg, 2, "navy", 0, True), (6, other, 1, "red", 100, False)])
    assert_doc(s.ok("GET", "/flags/1/variants"), [{"id": 1, "key": "red"}])
    assert_doc(s.ok("GET", f"/flags/1/segments/{seg}/distributions"), [{"id": 4, "percent": 100, "variantID": 1, "variantKey": "red"}])
    assert snapshot_count(s, 1) == before + 1
    assert s.call("DELETE", "/flags/999/variants/1").status == 404


def test_segments_create_update_and_reorder(flagr):
    s = flagr(recorder=False)
    two_flags(s)
    assert_doc(s.ok("POST", "/flags/1/segments", {"description": "s1", "rolloutPercent": 50}),
               {"id": 1, "description": "s1", "rank": 999, "rolloutPercent": 50, "constraints": [], "distributions": []})
    s.ok("POST", "/flags/1/segments", {"description": "s2", "rolloutPercent": 100})
    s.ok("POST", "/flags/1/segments", {"description": "s3", "rolloutPercent": 0})
    s.ok("POST", "/flags/2/segments", {"description": "g", "rolloutPercent": 10})

    def seg(i, d, rank, roll):
        return {"id": i, "description": d, "rank": rank, "rolloutPercent": roll, "constraints": [], "distributions": []}

    assert_doc(s.ok("GET", "/flags/1/segments"), [seg(1, "s1", 999, 50), seg(2, "s2", 999, 100), seg(3, "s3", 999, 0)])
    assert_doc(s.ok("PUT", "/flags/1/segments/1", {"description": "s1 edited", "rolloutPercent": 30}), seg(1, "s1 edited", 999, 30))
    assert s.call("PUT", "/flags/1/segments/reorder", {"segmentIDs": [3, 1]}).status == 200
    # Ranks are list positions; segments missing from the list keep their rank.
    assert_doc(s.ok("GET", "/flags/1/segments"), [seg(3, "s3", 0, 0), seg(1, "s1 edited", 1, 30), seg(2, "s2", 999, 100)])
    assert_rows(s.rows(SEGMENT_ROWS), [(1, 1, "s1 edited", 1, 30, False), (2, 1, "s2", 999, 100, False),
                                       (3, 1, "s3", 0, 0, False), (4, 2, "g", 999, 10, False)])
    assert_doc(s.ok("GET", "/flags/1")["segments"], [seg(3, "s3", 0, 0), seg(1, "s1 edited", 1, 30), seg(2, "s2", 999, 100)])
    assert snapshot_count(s, 1) == 6


def test_segment_rejections_preserve_rows(flagr):
    s = flagr(recorder=False)
    two_flags(s)
    s.ok("POST", "/flags/1/segments", {"description": "s1", "rolloutPercent": 50})
    s.ok("POST", "/flags/1/segments", {"description": "s2", "rolloutPercent": 60})
    s.ok("POST", "/flags/2/segments", {"description": "g", "rolloutPercent": 10})
    before = s.rows(SEGMENT_ROWS)
    for method, path, body, status in [
        ("POST", "/flags/1/segments", {"description": "s", "rolloutPercent": 101}, 422),
        ("POST", "/flags/1/segments", {"description": "s", "rolloutPercent": -1}, 422),
        ("POST", "/flags/1/segments", {"description": "s"}, 422),
        ("POST", "/flags/1/segments", {"description": "", "rolloutPercent": 5}, 422),
        ("PUT", "/flags/1/segments/1", {"description": "x", "rolloutPercent": 101}, 422),
        ("PUT", "/flags/1/segments/1", {"description": "x"}, 422),
        ("PUT", "/flags/1/segments/3", {"description": "not mine", "rolloutPercent": 30}, 500),
        ("PUT", "/flags/1/segments/77", {"description": "nope", "rolloutPercent": 30}, 500),
        ("PUT", "/flags/1/segments/reorder", {"segmentIDs": [2, 77]}, 404),
        ("PUT", "/flags/1/segments/reorder", {"segmentIDs": []}, 422),
        ("DELETE", "/flags/1/segments/3", None, 500),
        ("POST", "/flags/999/segments", {"description": "orphan", "rolloutPercent": 10}, 500),
    ]:
        r = s.call(method, path, body)
        assert r.status == status, (method, path, body, r)
        assert_rows(s.rows(SEGMENT_ROWS), before)
    assert snapshot_count(s, 1) == 3


def test_delete_segment_is_soft_and_leaves_children(flagr):
    s = flagr(recorder=False)
    f = s.flag("f1", variants=["a"])
    keep = s.segment(f, "keep", 100, [("a", 100)])
    gone = s.segment(f, "gone", 100, [("a", 100)], [("x", "EQ", '"y"')])
    assert s.call("DELETE", f"/flags/1/segments/{gone}").status == 200
    assert_rows(s.rows(SEGMENT_ROWS), [(keep, 1, "keep", 999, 100, False), (gone, 1, "gone", 999, 100, True)])
    assert_rows(s.rows("SELECT id, segment_id, deleted_at IS NOT NULL FROM constraints"), [(1, gone, False)])
    assert_rows(s.rows("SELECT id, segment_id, deleted_at IS NOT NULL FROM distributions ORDER BY id"), [(1, keep, False), (2, gone, False)])
    assert [x["id"] for x in s.ok("GET", "/flags/1/segments")] == [keep]
    assert [x["id"] for x in s.ok("GET", "/flags/1")["segments"]] == [keep]
    # Deleting it again finds no segment of this flag.
    assert s.call("DELETE", f"/flags/1/segments/{gone}").status == 500


def test_constraints_create_update_delete(flagr):
    s = flagr(recorder=False)
    two_flags(s)
    s.ok("POST", "/flags/1/segments", {"description": "s1", "rolloutPercent": 100})
    s.ok("POST", "/flags/2/segments", {"description": "g", "rolloutPercent": 100})
    c1 = s.ok("POST", "/flags/1/segments/1/constraints", {"property": "state", "operator": "EQ", "value": '"CA"'})
    assert_doc(c1, {"id": 1, "property": "state", "operator": "EQ", "value": '"CA"'})
    s.ok("POST", "/flags/1/segments/1/constraints", {"property": "age", "operator": "GT", "value": "18"})
    s.ok("POST", "/flags/1/segments/1/constraints", {"property": "user.tags[0]", "operator": "EREG", "value": '"^vip-\\d+$"'})
    s.ok("POST", "/flags/2/segments/2/constraints", {"property": "x", "operator": "NOTIN", "value": '[1, 2]'})
    expected = [{"id": 1, "property": "state", "operator": "EQ", "value": '"CA"'},
                {"id": 2, "property": "age", "operator": "GT", "value": "18"},
                {"id": 3, "property": "user.tags[0]", "operator": "EREG", "value": '"^vip-\\d+$"'}]
    assert_doc(s.ok("GET", "/flags/1/segments/1/constraints"), expected)
    assert_doc(s.ok("GET", "/flags/1")["segments"][0]["constraints"], expected)
    updated = s.ok("PUT", "/flags/1/segments/1/constraints/1", {"property": "state", "operator": "IN", "value": '["CA", "NY"]'})
    assert_doc(updated, {"id": 1, "property": "state", "operator": "IN", "value": '["CA", "NY"]'})
    assert s.call("DELETE", "/flags/1/segments/1/constraints/2").status == 200
    assert_rows(s.rows(CONSTRAINT_ROWS), [(1, 1, "state", "IN", '["CA", "NY"]', False), (2, 1, "age", "GT", "18", True),
                                          (3, 1, "user.tags[0]", "EREG", '"^vip-\\d+$"', False), (4, 2, "x", "NOTIN", "[1, 2]", False)])
    assert_doc(s.ok("GET", "/flags/1/segments/1/constraints"),
               [{"id": 1, "property": "state", "operator": "IN", "value": '["CA", "NY"]'}, expected[2]])
    assert snapshot_count(s, 1) == 7


def test_constraint_rejections_preserve_rows(flagr):
    s = flagr(recorder=False)
    two_flags(s)
    s.ok("POST", "/flags/1/segments", {"description": "s1", "rolloutPercent": 100})
    s.ok("POST", "/flags/2/segments", {"description": "g", "rolloutPercent": 100})
    s.ok("POST", "/flags/1/segments/1/constraints", {"property": "state", "operator": "EQ", "value": '"CA"'})
    s.ok("POST", "/flags/2/segments/2/constraints", {"property": "x", "operator": "EQ", "value": "1"})
    before = s.rows(CONSTRAINT_ROWS)
    bad_bodies = [
        ({"property": "s", "operator": "EQ", "value": "NY"}, 400),          # unquoted string
        ({"property": "s", "operator": "IN", "value": "[NY]"}, 400),        # unquoted list item
        ({"property": "s", "operator": "IN", "value": '["CA", 5]'}, 400),   # mixed list
        ({"property": "s", "operator": "EQ", "value": '"CA"]'}, 400),
        ({"property": "s", "operator": "===", "value": '"CA"'}, 400),
        ({"property": "s", "operator": "eq", "value": '"CA"'}, 400),
        ({"property": "a b", "operator": "EQ", "value": '"x"'}, 400),
        ({"property": "s", "operator": "GT", "value": "abc"}, 400),
        ({"property": "s", "operator": "EQ", "value": ""}, 422),
        ({"property": "", "operator": "EQ", "value": '"CA"'}, 422),
        ({"property": "s", "value": '"CA"'}, 422),
    ]
    for body, status in bad_bodies:
        r = s.call("POST", "/flags/1/segments/1/constraints", body)
        assert r.status == status, (body, r)
        assert r.json and r.json.get("message"), r
        r = s.call("PUT", "/flags/1/segments/1/constraints/1", body)
        assert r.status == status, ("PUT", body, r)
        assert_rows(s.rows(CONSTRAINT_ROWS), before)
    for method, path, body, status in [
        ("PUT", "/flags/1/segments/1/constraints/99", {"property": "s", "operator": "EQ", "value": '"CA"'}, 404),
        ("PUT", "/flags/1/segments/1/constraints/2", {"property": "s", "operator": "EQ", "value": '"ZZ"'}, 500),
        ("DELETE", "/flags/1/segments/1/constraints/2", None, 500),
        ("DELETE", "/flags/1/segments/1/constraints/99", None, 500),
    ]:
        r = s.call(method, path, body)
        assert r.status == status, (method, path, r)
        assert_rows(s.rows(CONSTRAINT_ROWS), before)
    assert snapshot_count(s, 1) == 3 and snapshot_count(s, 2) == 3


def test_distributions_replace_whole_segment(flagr):
    s = flagr(recorder=False)
    f = s.flag("f1", variants=["red", "blue", "green"])
    seg = s.segment(f, "s1", 100)
    put = s.ok("PUT", f"/flags/1/segments/{seg}/distributions", {"distributions": [
        {"variantID": 1, "variantKey": "red", "percent": 60}, {"variantID": 2, "variantKey": "blue", "percent": 40}]})
    assert_doc(put, [{"id": 1, "percent": 60, "variantID": 1, "variantKey": "red"}, {"id": 2, "percent": 40, "variantID": 2, "variantKey": "blue"}])
    put = s.ok("PUT", f"/flags/1/segments/{seg}/distributions", {"distributions": [
        {"variantID": 3, "variantKey": "green", "percent": 70}, {"variantID": 1, "variantKey": "red", "percent": 30}]})
    # The response keeps request order; listings order by variant.
    assert_doc(put, [{"id": 3, "percent": 70, "variantID": 3, "variantKey": "green"}, {"id": 4, "percent": 30, "variantID": 1, "variantKey": "red"}])
    listed = [{"id": 4, "percent": 30, "variantID": 1, "variantKey": "red"}, {"id": 3, "percent": 70, "variantID": 3, "variantKey": "green"}]
    assert_doc(s.ok("GET", f"/flags/1/segments/{seg}/distributions"), listed)
    assert_doc(s.ok("GET", "/flags/1")["segments"][0]["distributions"], listed)
    # Replaced rows are soft-deleted, not updated in place.
    assert_rows(s.rows(DIST_ROWS), [(1, seg, 1, "red", 60, True), (2, seg, 2, "blue", 40, True),
                                    (3, seg, 3, "green", 70, False), (4, seg, 1, "red", 30, False)])
    assert snapshot_count(s, 1) == 7


def test_distribution_rejections_preserve_rows(flagr):
    s = flagr(recorder=False)
    f = s.flag("f1", variants=["red", "blue"])
    g = s.flag("g1", variants=["gv"])
    seg = s.segment(f, "s1", 100, [("red", 60), ("blue", 40)])
    gseg = s.segment(g, "g", 100)
    before = s.rows(DIST_ROWS)
    for path, dists, status in [
        (f"/flags/1/segments/{seg}/distributions", [{"variantID": 1, "variantKey": "red", "percent": 60}, {"variantID": 2, "variantKey": "blue", "percent": 30}], 400),
        (f"/flags/1/segments/{seg}/distributions", [{"variantID": 1, "variantKey": "red", "percent": 100}, {"variantID": 2, "variantKey": "blue", "percent": 1}], 400),
        (f"/flags/1/segments/{seg}/distributions", [], 400),
        (f"/flags/1/segments/{seg}/distributions", [{"variantID": 1, "variantKey": "blue", "percent": 100}], 400),
        (f"/flags/1/segments/{seg}/distributions", [{"variantID": 3, "variantKey": "gv", "percent": 100}], 400),
        (f"/flags/1/segments/{seg}/distributions", [{"variantID": 1, "variantKey": "red", "percent": 101}], 422),
        (f"/flags/1/segments/{seg}/distributions", [{"variantID": 1, "variantKey": "red"}], 422),
        (f"/flags/1/segments/{gseg}/distributions", [{"variantID": 1, "variantKey": "red", "percent": 100}], 500),
        (f"/flags/999/segments/{seg}/distributions", [{"variantID": 1, "variantKey": "red", "percent": 100}], 400),
    ]:
        r = s.call("PUT", path, {"distributions": dists})
        assert r.status == status, (path, dists, r)
        assert_rows(s.rows(DIST_ROWS), before)
    assert s.call("PUT", f"/flags/1/segments/{seg}/distributions", {}).status == 422
    assert_rows(s.rows(DIST_ROWS), before)
    assert snapshot_count(s, 1) == 5


def test_tags_attach_list_and_detach(flagr):
    s = flagr(recorder=False)
    two_flags(s)
    assert_doc(s.ok("POST", "/flags/1/tags", {"value": "team-a"}), {"id": 1, "value": "team-a"})
    # Attaching an existing tag reuses it.
    assert_doc(s.ok("POST", "/flags/1/tags", {"value": "team-a"}), {"id": 1, "value": "team-a"})
    assert_doc(s.ok("POST", "/flags/1/tags", {"value": "Env:Prod/1.0"}), {"id": 2, "value": "Env:Prod/1.0"})
    assert_doc(s.ok("POST", "/flags/1/tags", {"value": "with space"}), {"id": 3, "value": "with space"})
    assert_doc(s.ok("POST", "/flags/2/tags", {"value": "team-a"}), {"id": 1, "value": "team-a"})
    assert_doc(s.ok("POST", "/flags/2/tags", {"value": "beta"}), {"id": 4, "value": "beta"})
    tags = [{"id": 1, "value": "team-a"}, {"id": 2, "value": "Env:Prod/1.0"}, {"id": 3, "value": "with space"}, {"id": 4, "value": "beta"}]
    assert_doc(s.ok("GET", "/flags/1/tags"), tags[:3])
    assert_doc(s.ok("GET", "/flags/1")["tags"], tags[:3])
    assert_doc(s.ok("GET", "/tags"), tags)
    assert_doc(s.ok("GET", "/tags", query={"value_like": "TEAM"}), tags[:1])
    assert_doc(s.ok("GET", "/tags", query={"value_like": "a"}), [tags[0], tags[2], tags[3]])
    assert_doc(s.ok("GET", "/tags", query={"limit": 2, "offset": 1}), tags[1:3])
    assert_rows(s.rows("SELECT id, value FROM tags ORDER BY id"), [(t["id"], t["value"]) for t in tags])
    assert_rows(s.rows("SELECT flag_id, tag_id FROM flags_tags ORDER BY 1, 2"), [(1, 1), (1, 2), (1, 3), (2, 1), (2, 4)])
    # Detaching removes only the association; the tag itself stays listed.
    assert s.call("DELETE", "/flags/1/tags/1").status == 200
    assert_doc(s.ok("GET", "/flags/1/tags"), tags[1:3])
    assert_doc(s.ok("GET", "/flags/2/tags"), [tags[0], tags[3]])
    assert_doc(s.ok("GET", "/tags"), tags)
    assert_rows(s.rows("SELECT flag_id, tag_id FROM flags_tags ORDER BY 1, 2"), [(1, 2), (1, 3), (2, 1), (2, 4)])
    # Detaching something not attached still succeeds.
    assert s.call("DELETE", "/flags/1/tags/1").status == 200
    assert s.call("DELETE", "/flags/1/tags/77").status == 200
    assert_rows(s.rows("SELECT flag_id, count(*) FROM flag_snapshots GROUP BY flag_id ORDER BY 1"), [(1, 8), (2, 3)])


def test_tag_rejections_preserve_rows(flagr):
    s = flagr(recorder=False)
    two_flags(s)
    s.ok("POST", "/flags/1/tags", {"value": "team-a"})
    for path, body, status in [
        ("/flags/1/tags", {"value": "bad!tag"}, 400),
        ("/flags/1/tags", {"value": "t" * 64}, 400),
        ("/flags/1/tags", {"value": ""}, 422),
        ("/flags/1/tags", {}, 422),
        ("/flags/999/tags", {"value": "ghost"}, 500),
    ]:
        r = s.call("POST", path, body)
        assert r.status == status, (path, body, r)
        assert_rows(s.rows("SELECT id, value FROM tags"), [(1, "team-a")])
        assert_rows(s.rows("SELECT flag_id, tag_id FROM flags_tags"), [(1, 1)])
    assert s.ok("POST", "/flags/2/tags", {"value": "t" * 63})["value"] == "t" * 63


def test_every_successful_mutation_writes_one_snapshot_of_the_result(flagr):
    s = flagr(recorder=False)
    steps = [
        ("POST", "/flags", {"description": "d", "key": "f1"}),
        ("PUT", "/flags/1", {"description": "d2", "notes": "x"}),
        ("PUT", "/flags/1/enabled", {"enabled": True}),
        ("POST", "/flags/1/variants", {"key": "a"}),
        ("POST", "/flags/1/variants", {"key": "b", "attachment": {"k": "v"}}),
        ("PUT", "/flags/1/variants/2", {"key": "bb"}),
        ("POST", "/flags/1/segments", {"description": "s1", "rolloutPercent": 50}),
        ("POST", "/flags/1/segments", {"description": "s2", "rolloutPercent": 70}),
        ("PUT", "/flags/1/segments/1", {"description": "s1x", "rolloutPercent": 40}),
        ("PUT", "/flags/1/segments/reorder", {"segmentIDs": [2, 1]}),
        ("POST", "/flags/1/segments/1/constraints", {"property": "p", "operator": "EQ", "value": '"v"'}),
        ("PUT", "/flags/1/segments/1/constraints/1", {"property": "p", "operator": "NEQ", "value": '"w"'}),
        ("PUT", "/flags/1/segments/1/distributions", {"distributions": [{"variantID": 1, "variantKey": "a", "percent": 100}]}),
        ("POST", "/flags/1/tags", {"value": "t1"}),
        ("DELETE", "/flags/1/tags/1", None),
        ("DELETE", "/flags/1/segments/1/constraints/1", None),
        ("DELETE", "/flags/1/segments/2", None),
        ("DELETE", "/flags/1/variants/2", None),
    ]
    for count, (method, path, body) in enumerate(steps, start=1):
        assert s.call(method, path, body).status == 200, (method, path)
        assert_rows(s.rows("SELECT count(*), max(id) FROM flag_snapshots WHERE flag_id = 1"), [(count, count)])
        assert_rows(s.rows("SELECT snapshot_id FROM flags WHERE id = 1"), [(count,)])
        latest = s.ok("GET", "/flags/1/snapshots", query={"limit": 1})
        assert len(latest) == 1 and latest[0]["id"] == count, latest
        assert_doc(latest[0]["flag"], s.ok("GET", "/flags/1"), what=f"snapshot after {method} {path}")
    assert s.call("DELETE", "/flags/1").status == 200
    assert_rows(s.rows("SELECT count(*) FROM flag_snapshots WHERE flag_id = 1"), [(len(steps) + 1,)])
    assert s.ok("GET", "/flags/snapshots/max_id") == {"maxID": len(steps) + 1}
