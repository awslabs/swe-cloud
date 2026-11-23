"""Targeting query language, one rule per flag, against four evaluation contexts.

Each row: query, then for contexts C1..C4 whether the rule matched (Y: TARGETING_MATCH with
evaluatedRuleName, N: DEFAULT); "invalid" means the definition is rejected (404 FLAG_NOT_FOUND).
Native observations: experiments/scen_query.py (curation.md). Several rows adapt upstream
modules/core/flag/rule_test.go cases.
"""

import pytest

from relaykit import assert_response

CONTEXTS = [
    {"targetingKey": "aae1cb41", "email": "ann@test.com", "role": "backend engineer", "age": 30, "plan": "pro",
     "beta": True, "company": {"name": "Acme", "size": 120}, "tags": ["a", "b"], "score": 7.5, "country": "FR", "name": "Ann"},
    {"targetingKey": "user-2", "email": "bob@example.com", "age": "18", "plan": "free", "beta": False,
     "company": {"name": "acme"}, "country": "US", "anonymous": True},
    {"targetingKey": "x"},
    {"targetingKey": "u4", "age": 17, "email": "ANN@TEST.COM", "tags": [], "plan": None, "name": "ann", "score": 8,
     "beta": "true", "country": "US"},
]

QUERIES = [
    ('targetingKey eq "aae1cb41"', 'YNNN'),
    ('key eq "aae1cb41"', 'YNNN'),
    ('email ew "@test.com"', 'YNNY'),
    ('email sw "ann"', 'YNNY'),
    ('email co "test"', 'YNNY'),
    ('email eq "ann@test.com"', 'YNNY'),
    ('email EQ "ann@test.com"', 'YNNY'),
    ('email == "ann@test.com"', 'YNNY'),
    ('email ne "ann@test.com"', 'NYNN'),
    ('email != "ann@test.com"', 'NYNN'),
    ('age gt 17', 'YNNN'),
    ('age > 17', 'YNNN'),
    ('age ge 18', 'YNNN'),
    ('age lt 18', 'NNNY'),
    ('age le 30', 'YNNY'),
    ('age eq 30', 'YNNN'),
    ('age eq "30"', 'NNNN'),
    ('score gt 7', 'NNNY'),
    ('score eq 7.5', 'YNNN'),
    ('beta eq true', 'YNNN'),
    ('beta == true', 'YNNN'),
    ('beta ne true', 'NYNN'),
    ('plan in ["pro","enterprise"]', 'YNNN'),
    ('plan in ["PRO"]', 'NNNN'),
    ('country pr', 'YYNY'),
    ('nickname pr', 'NNNN'),
    ('not (country eq "FR")', 'NYYY'),
    ('age ge 18 and not (country eq "FR")', 'NNNN'),
    ('(email ew "@test.com") and (role eq "backend engineer")', 'YNNN'),
    ('plan eq "pro" or beta eq false', 'YYNN'),
    ('company.name eq "Acme"', 'YYNN'),
    ('company.size gt 100', 'YNNN'),
    ('tags co "a"', 'NNNN'),
    ('name eq "ann"', 'YNNY'),
    ('email ew "@TEST.COM"', 'YNNY'),
    ('age in [17, 30]', 'YNNY'),
    ('email ge "b"', 'NYNN'),
    ('targetingKey sw "user"', 'NYNN'),
    ('plan eq "pro" and beta eq true or country eq "US"', 'YYNY'),
    ('plan eq "free" or beta eq true and country eq "US"', 'NYNN'),
    ('anonymous eq true', 'NYNN'),
    ("email eq 'ann@test.com'", 'invalid'),
    (' email ew "@test.com"\n and age gt 1 ', 'YNNY'),
    ('', 'invalid'),
    ('email eq', 'invalid'),
    ('age gt', 'invalid'),
    ('(beta eq true', 'YNNN'),
    ('foo bar baz', 'invalid'),
    ('email EW "@test.com" AND age GT 1', 'YNNY'),
    ('NOT (beta eq true)', 'NYYY'),
    ('age ne 30', 'NNNY'),
    ('company.name pr', 'YYNN'),
    ('company.missing pr', 'NNNN'),
    ('score lt 8', 'YNNN'),
    ('age eq 30.0', 'YNNN'),
    ('tags in ["a"]', 'NNNN'),
    ('email co "TEST"', 'YNNY'),
    ('plan eq null', 'NNYY'),
    ('country in ["FR", "US"] and not (plan eq "free")', 'YNNY'),
    ('name sw "A" and name ew "n"', 'YNNY'),
    ('age gt 17 or age gt "17"', 'YYNN'),
    ('beta eq "true"', 'NNNY'),
    ('targetingKey ne ""', 'YYYY'),
]

FLAGS = {
    f"q{i:02d}": {"variations": {"yes": "Y", "no": "N"},
                  "targeting": [{"name": "r", "query": query, "variation": "yes"}],
                  "defaultRule": {"variation": "no"}}
    for i, (query, _) in enumerate(QUERIES)
}


@pytest.mark.parametrize(("index", "query", "outcome"), [(i, q, o) for i, (q, o) in enumerate(QUERIES)],
                         ids=[f"q{i:02d}" for i in range(len(QUERIES))])
def test_query(module_relay, index, query, outcome):
    key = f"q{index:02d}"
    for n, context in enumerate(CONTEXTS, start=1):
        response = module_relay.ofrep(key, context)
        if outcome == "invalid":
            expected = (404, {"key": key, "errorCode": "FLAG_NOT_FOUND", "errorDetails": "*"})
        elif outcome[n - 1] == "Y":
            expected = (200, {"key": key, "value": "Y", "variant": "yes", "reason": "TARGETING_MATCH",
                              "metadata": {"evaluatedRuleName": "r", "gofeatureflag_cacheable": True}})
        else:
            expected = (200, {"key": key, "value": "N", "variant": "no", "reason": "DEFAULT",
                              "metadata": {"gofeatureflag_cacheable": True}})
        assert_response(response, *expected, label=f"{query!r} with C{n}")
