"""Flag resources: creation, update, listing, soft delete/restore, duplication and
the snapshot history, checked through the API and the stored rows."""

import re

from flagr_support import assert_doc, assert_rows

FLAG_ROW = ("SELECT id, key, description, enabled, data_records_enabled, COALESCE(entity_type, ''), COALESCE(notes, ''), "
            "deleted_at IS NOT NULL FROM flags ORDER BY id")
RANDOM_KEY = re.compile(r"^k[1-9a-km-z]{16}$")


def empty_flag(fid, key, description, **fields):
    return {"id": fid, "key": key, "description": description, "enabled": False, "dataRecordsEnabled": False,
            "segments": [], "variants": [], "tags": [], **fields}


def snapshot_counts(server):
    return server.rows("SELECT flag_id, count(*) FROM flag_snapshots GROUP BY flag_id ORDER BY flag_id")


def test_create_flag_returns_and_stores_flag(flagr):
    s = flagr(recorder=False)
    r = s.call("POST", "/flags", {"description": "first flag", "key": "alpha"})
    assert r.status == 200
    assert_doc(r.json, empty_flag(1, "alpha", "first flag"))
    assert isinstance(r.json.get("updatedAt"), str)
    auto = s.ok("POST", "/flags", {"description": "auto key"})
    assert auto["id"] == 2 and RANDOM_KEY.match(auto["key"]), auto
    assert_doc(auto, empty_flag(2, auto["key"], "auto key"))
    assert_rows(s.rows(FLAG_ROW), [(1, "alpha", "first flag", False, False, "", "", False),
                                   (2, auto["key"], "auto key", False, False, "", "", False)])
    # Each creation stores one snapshot and links the flag to it.
    assert_rows(s.rows("SELECT id, flag_id FROM flag_snapshots ORDER BY id"), [(1, 1), (2, 2)])
    assert_rows(s.rows("SELECT id, snapshot_id FROM flags ORDER BY id"), [(1, 1), (2, 2)])
    assert_doc(s.ok("GET", "/flags/1"), empty_flag(1, "alpha", "first flag"))
    assert s.ok("GET", "/flags/snapshots/max_id") == {"maxID": 2}


def test_simple_boolean_template_creates_variant_segment_and_distribution(flagr):
    s = flagr(recorder=False)
    f = s.ok("POST", "/flags", {"description": "tmpl", "key": "boolflag", "template": "simple_boolean_flag"})
    expected = empty_flag(1, "boolflag", "tmpl",
                          variants=[{"id": 1, "key": "on"}],
                          segments=[{"id": 1, "description": "", "rank": 999, "rolloutPercent": 100, "constraints": [],
                                     "distributions": [{"id": 1, "percent": 100, "variantID": 1, "variantKey": "on"}]}])
    assert_doc(f, expected)
    assert_doc(s.ok("GET", "/flags/1"), expected)
    assert_rows(s.rows("SELECT id, flag_id, key FROM variants"), [(1, 1, "on")])
    assert_rows(s.rows("SELECT id, flag_id, rank, rollout_percent FROM segments"), [(1, 1, 999, 100)])
    assert_rows(s.rows("SELECT segment_id, variant_id, variant_key, percent FROM distributions"), [(1, 1, "on", 100)])
    # The whole template is one mutation: one snapshot.
    assert_rows(snapshot_counts(s), [(1, 1)])
    r = s.call("POST", "/flags", {"description": "tmpl", "key": "other", "template": "nope"})
    assert r.status == 400
    assert_rows(s.rows("SELECT count(*) FROM flags"), [(1,)])


def test_create_flag_rejections_leave_no_rows(flagr):
    s = flagr(recorder=False)
    s.ok("POST", "/flags", {"description": "first", "key": "alpha"})
    cases = [
        ({"description": "dup key", "key": "alpha"}, 400),
        ({"description": "bad key", "key": "Bad Key!"}, 400),
        ({"description": "long key", "key": "k" * 64}, 400),
        ({"description": "", "key": "emptydesc"}, 422),
        ({"key": "nodesc"}, 422),
    ]
    for body, status in cases:
        r = s.call("POST", "/flags", body)
        assert r.status == status, (body, r)
        assert isinstance(r.json, dict) and isinstance(r.json.get("message"), str) and r.json["message"], r
        assert_rows(s.rows("SELECT key FROM flags"), [("alpha",)])
    r = s.call("POST", "/flags", raw=b"not json")
    assert r.status == 400, r
    assert_rows(s.rows("SELECT count(*) FROM flag_snapshots"), [(1,)])
    assert s.ok("POST", "/flags", {"description": "max key", "key": "k" * 63})["key"] == "k" * 63


def test_put_flag_updates_fields_and_ignores_enabled(flagr):
    s = flagr(recorder=False)
    s.ok("POST", "/flags", {"description": "first flag", "key": "alpha"})
    s.ok("POST", "/flags", {"description": "other", "key": "beta"})
    updated = s.ok("PUT", "/flags/1", {"description": "renamed", "dataRecordsEnabled": True, "entityType": "user",
                                        "notes": "some *notes*"})
    expected = empty_flag(1, "alpha", "renamed", dataRecordsEnabled=True, entityType="user", notes="some *notes*")
    assert_doc(updated, expected)
    # `enabled` is not changed through PUT /flags/{id}, but the request still succeeds.
    assert_doc(s.ok("PUT", "/flags/1", {"enabled": True}), expected)
    renamed = s.ok("PUT", "/flags/1", {"key": "alpha2"})
    expected["key"] = "alpha2"
    assert_doc(renamed, expected)
    assert_rows(s.rows(FLAG_ROW), [(1, "alpha2", "renamed", False, True, "user", "some *notes*", False),
                                   (2, "beta", "other", False, False, "", "", False)])
    assert s.ok("GET", "/flags/entity_types") == ["user"]
    assert_rows(s.rows("SELECT key FROM flag_entity_types"), [("user",)])
    assert_rows(snapshot_counts(s), [(1, 4), (2, 1)])


def test_put_flag_rejections_preserve_flag(flagr):
    s = flagr(recorder=False)
    s.ok("POST", "/flags", {"description": "first flag", "key": "alpha"})
    s.ok("POST", "/flags", {"description": "other", "key": "beta"})
    before = s.rows(FLAG_ROW)
    for path, body, status in [
        ("/flags/1", {"key": "beta"}, 400),
        ("/flags/1", {"key": "bad key"}, 400),
        ("/flags/1", {"entityType": "bad type!"}, 500),
        ("/flags/1", {"description": ""}, 422),
        ("/flags/999", {"description": "x"}, 404),
        ("/flags/abc", {"description": "x"}, 422),
    ]:
        r = s.call("PUT", path, body)
        assert r.status == status, (path, body, r)
        assert_rows(s.rows(FLAG_ROW), before)
    assert_rows(snapshot_counts(s), [(1, 1), (2, 1)])
    assert s.ok("GET", "/flags/entity_types") == []


def test_set_enabled_toggles_and_validates(flagr):
    s = flagr(recorder=False)
    s.ok("POST", "/flags", {"description": "first flag", "key": "alpha"})
    on = s.ok("PUT", "/flags/1/enabled", {"enabled": True})
    assert_doc(on, empty_flag(1, "alpha", "first flag", enabled=True))
    assert_rows(s.rows("SELECT enabled FROM flags"), [(True,)])
    assert s.call("PUT", "/flags/1/enabled", {}).status == 422
    assert s.call("PUT", "/flags/1/enabled", {"enabled": "yes"}).status == 400
    assert s.call("PUT", "/flags/999/enabled", {"enabled": True}).status == 404
    assert_rows(s.rows("SELECT enabled FROM flags"), [(True,)])
    off = s.ok("PUT", "/flags/1/enabled", {"enabled": False})
    assert off["enabled"] is False
    assert_rows(s.rows("SELECT enabled FROM flags"), [(False,)])
    assert_rows(snapshot_counts(s), [(1, 3)])


def test_get_flag_errors(flagr):
    s = flagr(recorder=False)
    r = s.call("GET", "/flags/999")
    assert r.status == 404 and r.json["message"], r
    assert s.call("GET", "/flags/abc").status == 422
    assert s.ok("GET", "/flags") == []
    assert_doc(s.ok("GET", "/flags/snapshots/max_id"), {"maxID": 0})
    assert s.ok("GET", "/flags/entity_types") == []
    assert s.ok("GET", "/tags") == []


def test_list_flags_filters(flagr):
    s = flagr(recorder=False)
    s.ok("POST", "/flags", {"description": "Checkout redesign", "key": "checkout"})
    s.ok("POST", "/flags", {"description": "search ranking", "key": "search"})
    s.ok("POST", "/flags", {"description": "tmpl", "key": "boolflag", "template": "simple_boolean_flag"})
    s.ok("PUT", "/flags/2/enabled", {"enabled": True})
    s.ok("POST", "/flags/1/tags", {"value": "web"})
    s.ok("POST", "/flags/3/tags", {"value": "web"})
    s.ok("POST", "/flags/3/tags", {"value": "mobile"})
    web, mobile = {"id": 1, "value": "web"}, {"id": 2, "value": "mobile"}
    # Listing omits segments and variants unless preload is requested; tags are always included.
    f1 = empty_flag(1, "checkout", "Checkout redesign", tags=[web])
    f2 = empty_flag(2, "search", "search ranking", enabled=True)
    f3 = empty_flag(3, "boolflag", "tmpl", tags=[web, mobile])

    def listed(query, expected):
        got = s.ok("GET", "/flags", query=query)
        assert_doc(got, expected, what=f"GET /flags {query}")

    listed(None, [f1, f2, f3])
    listed({"enabled": "true"}, [f2])
    listed({"enabled": "false"}, [f1, f3])
    listed({"key": "search"}, [f2])
    listed({"description": "tmpl"}, [f3])
    listed({"description": "checkout"}, [])
    listed({"description_like": "CHECKOUT"}, [f1])
    listed({"description_like": "r"}, [f1, f2])
    listed({"limit": 1, "offset": 1}, [f2])
    listed({"limit": 2}, [f1, f2])
    listed({"tags": "mobile"}, [f3])
    listed({"tags": "web"}, [f1, f3])
    listed({"tags": "mobile,web"}, [f1, f3])
    listed({"key": "boolflag", "preload": "true"}, [dict(f3, variants=[{"id": 1, "key": "on"}], segments=[
        {"id": 1, "description": "", "rank": 999, "rolloutPercent": 100, "constraints": [],
         "distributions": [{"id": 1, "percent": 100, "variantID": 1, "variantKey": "on"}]}])])
    assert s.call("GET", "/flags", query={"limit": "x"}).status == 422


def test_soft_delete_and_restore(flagr):
    s = flagr(recorder=False)
    s.ok("POST", "/flags", {"description": "keep", "key": "keep"})
    s.ok("POST", "/flags", {"description": "gone", "key": "gone"})
    s.ok("POST", "/flags/2/variants", {"key": "v"})
    assert s.call("DELETE", "/flags/2").status == 200
    assert_rows(s.rows(FLAG_ROW), [(1, "keep", "keep", False, False, "", "", False),
                                   (2, "gone", "gone", False, False, "", "", True)])
    # Children are not touched by a flag delete.
    assert_rows(s.rows("SELECT id, flag_id, deleted_at IS NOT NULL FROM variants"), [(1, 2, False)])
    assert s.call("GET", "/flags/2").status == 404
    assert s.call("DELETE", "/flags/2").status == 404
    assert s.call("DELETE", "/flags/999").status == 404
    gone = empty_flag(2, "gone", "gone")
    assert_doc(s.ok("GET", "/flags"), [empty_flag(1, "keep", "keep")])
    assert_doc(s.ok("GET", "/flags", query={"deleted": "true"}), [gone])
    # The deletion is itself recorded in the history.
    assert_rows(snapshot_counts(s), [(1, 1), (2, 3)])
    restored = s.ok("PUT", "/flags/2/restore")
    assert_doc(restored, gone)
    assert_rows(s.rows("SELECT id, deleted_at IS NOT NULL FROM flags ORDER BY id"), [(1, False), (2, False)])
    assert_doc(s.ok("GET", "/flags/2"), dict(gone, variants=[{"id": 1, "key": "v"}]))
    assert_doc(s.ok("GET", "/flags", query={"deleted": "true"}), [])
    # Restoring a live flag succeeds and is recorded too; unknown flags are 404.
    assert_doc(s.ok("PUT", "/flags/1/restore"), empty_flag(1, "keep", "keep"))
    assert s.call("PUT", "/flags/999/restore").status == 404
    assert_rows(snapshot_counts(s), [(1, 2), (2, 4)])
    # A deleted flag still owns its key.
    s.ok("DELETE", "/flags/1")
    assert s.call("POST", "/flags", {"description": "again", "key": "keep"}).status == 400
    assert_rows(s.rows("SELECT count(*) FROM flags"), [(2,)])


def test_snapshot_history_listing(flagr):
    s = flagr(recorder=False)
    s.ok("POST", "/flags", {"description": "first flag", "key": "alpha"})
    s.ok("POST", "/flags", {"description": "other", "key": "beta"})
    s.ok("PUT", "/flags/1", {"description": "renamed", "dataRecordsEnabled": True})
    s.ok("PUT", "/flags/1/enabled", {"enabled": True})
    v1 = empty_flag(1, "alpha", "first flag")
    v2 = empty_flag(1, "alpha", "renamed", dataRecordsEnabled=True)
    v3 = dict(v2, enabled=True)
    snaps = s.ok("GET", "/flags/1/snapshots")
    assert_doc(snaps, [{"id": 4, "flag": v3}, {"id": 3, "flag": v2}, {"id": 1, "flag": v1}])
    assert all(isinstance(x.get("updatedAt"), str) and x["updatedAt"] for x in snaps), snaps
    assert_doc(s.ok("GET", "/flags/1/snapshots", query={"sort": "ASC"}), [{"id": 1, "flag": v1}, {"id": 3, "flag": v2}, {"id": 4, "flag": v3}])
    assert_doc(s.ok("GET", "/flags/1/snapshots", query={"limit": 2, "sort": "ASC"}), [{"id": 1, "flag": v1}, {"id": 3, "flag": v2}])
    assert_doc(s.ok("GET", "/flags/1/snapshots", query={"limit": 1, "offset": 1}), [{"id": 3, "flag": v2}])
    assert_doc(s.ok("GET", "/flags/2/snapshots"), [{"id": 2, "flag": empty_flag(2, "beta", "other")}])
    assert s.ok("GET", "/flags/999/snapshots") == []
    assert s.ok("GET", "/flags/snapshots/max_id") == {"maxID": 4}
    assert_rows(s.rows("SELECT id, flag_id FROM flag_snapshots ORDER BY id"), [(1, 1), (2, 2), (3, 1), (4, 1)])
    assert_rows(s.rows("SELECT id, snapshot_id FROM flags ORDER BY id"), [(1, 4), (2, 2)])


def test_duplicate_copies_structure_under_new_ids(flagr):
    s = flagr(recorder=False)
    f = s.flag("src", description="source flag", variants=["red", ("blue", {"hex": "#00f", "n": [1, 2.5]})],
               records=True, entityType="device", notes="n1")
    s1 = s.segment(f, "first", 30, [("red", 40), ("blue", 60)], [("state", "IN", '["CA","NY"]'), ("age", "GT", "18")])
    s2 = s.segment(f, "second", 100, [("blue", 100)])
    s.ok("PUT", f"/flags/{f['id']}/segments/reorder", {"segmentIDs": [s2, s1]})
    s.ok("POST", "/flags/1/tags", {"value": "web"})
    s.ok("PUT", "/flags/1/enabled", {"enabled": True})
    copy = s.ok("POST", "/flags/1/duplicate", {})
    assert RANDOM_KEY.match(copy["key"]), copy
    expected = {
        "id": 2, "key": copy["key"], "description": "source flag (cloned)", "enabled": True, "dataRecordsEnabled": True,
        "entityType": "device", "notes": "n1", "tags": [{"id": 1, "value": "web"}],
        "variants": [{"id": 3, "key": "red"}, {"id": 4, "key": "blue", "attachment": {"hex": "#00f", "n": [1, 2.5]}}],
        "segments": [
            {"id": 3, "description": "second", "rank": 0, "rolloutPercent": 100, "constraints": [],
             "distributions": [{"id": 4, "percent": 100, "variantID": 4, "variantKey": "blue"}]},
            {"id": 4, "description": "first", "rank": 1, "rolloutPercent": 30,
             "constraints": [{"id": 3, "property": "state", "operator": "IN", "value": '["CA","NY"]'},
                             {"id": 4, "property": "age", "operator": "GT", "value": "18"}],
             "distributions": [{"id": 5, "percent": 40, "variantID": 3, "variantKey": "red"},
                               {"id": 6, "percent": 60, "variantID": 4, "variantKey": "blue"}]},
        ],
    }
    assert_doc(copy, expected)
    assert_doc(s.ok("GET", "/flags/2"), expected)
    named = s.ok("POST", "/flags/1/duplicate", {"key": "src-copy", "description": "copy"})
    assert named["id"] == 3 and named["key"] == "src-copy" and named["description"] == "copy", named
    # The source is untouched; each duplicate is a single new snapshot.
    assert_rows(s.rows("SELECT flag_id, count(*) FROM variants GROUP BY flag_id ORDER BY 1"), [(1, 2), (2, 2), (3, 2)])
    assert_rows(s.rows("SELECT flag_id, count(*) FROM flags_tags GROUP BY flag_id ORDER BY 1"), [(1, 1), (2, 1), (3, 1)])
    assert_rows(snapshot_counts(s), [(1, 13), (2, 1), (3, 1)])
    for body, status in [({"key": "src-copy"}, 400), ({"key": "bad key"}, 400)]:
        assert s.call("POST", "/flags/1/duplicate", body).status == status
    assert s.call("POST", "/flags/999/duplicate", {}).status == 404
    assert_rows(s.rows("SELECT count(*) FROM flags"), [(3,)])


def test_duplicate_of_blank_description_and_disabled_flag(flagr):
    s = flagr(recorder=False)
    s.ok("POST", "/flags", {"description": " ", "key": "blank"})
    copy = s.ok("POST", "/flags/1/duplicate", {"description": "  "})
    assert_doc(copy, empty_flag(2, copy["key"], "(cloned)"))
