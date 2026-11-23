"""Shared helpers: run the program, decode its plans and inspect PostgreSQL catalogs.

Plan comparison follows the public observation rules in /docs/interface.md:
DDL text is compared after removing double quotes and insignificant whitespace,
temporary object names are matched by prefix with a consistent binding inside one
plan, hazards are compared as sets of types, timeouts by numeric value, and only
dependency relationships (not the reference's ordering of independent
statements) are checked.
"""

import itertools
import json
import re
from collections import Counter
from dataclasses import dataclass

from harness import run_program


ALL_HAZARDS = (
    "ACQUIRES_ACCESS_EXCLUSIVE_LOCK,ACQUIRES_SHARE_LOCK,ACQUIRES_SHARE_ROW_EXCLUSIVE_LOCK,AUTHZ_UPDATE,"
    "CORRECTNESS,DELETES_DATA,HAS_UNTRACKABLE_DEPENDENCIES,IMPACTS_DATABASE_PERFORMANCE,INDEX_BUILD,"
    "INDEX_DROPPED,IS_USER_GENERATED,UPGRADING_EXTENSION_VERSION"
)

_TEMP_TOKEN = re.compile(r"pgschemadiff_tmp(?:idx|nn)_[A-Za-z0-9_$]*")
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def norm(ddl):
    """Canonical DDL text: no double quotes, whitespace only between word characters, no trailing ';'."""
    text = ddl.replace('"', "")
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r" ?([^\w$ ]) ?", r"\1", text)
    return text.rstrip(";").rstrip()


@dataclass(frozen=True)
class S:
    """One expected plan statement, written as the reference prints it.

    Temporary names are written with a placeholder for their random part, e.g.
    ``pgschemadiff_tmpidx_t_ab_{a}`` or ``pgschemadiff_tmpnn_{n}``.
    """

    ddl: str
    timeout: int = 3000
    lock: int = 3000
    hazards: frozenset = frozenset()

    def __init__(self, ddl, timeout=3000, lock=3000, hazards=()):
        object.__setattr__(self, "ddl", norm(ddl))
        object.__setattr__(self, "timeout", timeout)
        object.__setattr__(self, "lock", lock)
        object.__setattr__(self, "hazards", frozenset(hazards))

    def key(self):
        return (self.ddl, self.timeout, self.lock, self.hazards)


def _number(value, what):
    assert type(value) in (int, float), f"{what} must be a JSON number, got {value!r}"
    return value


def statement_from_json(entry, where):
    assert isinstance(entry, dict), f"{where}: statement must be an object: {entry!r}"
    assert isinstance(entry.get("ddl"), str), f"{where}: ddl must be a string: {entry!r}"
    hazards = entry.get("hazards")
    assert hazards is None or isinstance(hazards, list), f"{where}: hazards must be null or a list: {entry!r}"
    types = []
    for hazard in hazards or []:
        assert isinstance(hazard, dict) and isinstance(hazard.get("type"), str), f"{where}: bad hazard {hazard!r}"
        types.append(hazard["type"])
    return {
        "ddl": norm(entry["ddl"]),
        "timeout": _number(entry.get("timeout_ms"), f"{where}: timeout_ms"),
        "lock": _number(entry.get("lock_timeout_ms"), f"{where}: lock_timeout_ms"),
        "hazards": frozenset(types),
        "raw": entry["ddl"],
    }


def decode_plan(stdout):
    """Decode a JSON plan; returns the list of statement dicts (empty for a null/empty plan)."""
    try:
        document = json.loads(stdout)
    except ValueError as error:
        raise AssertionError(f"plan output is not JSON: {error}\n{stdout[:2000]}") from None
    assert isinstance(document, dict), f"plan must be a JSON object: {stdout[:500]}"
    assert "statements" in document, f"plan has no statements key: {sorted(document)}"
    assert isinstance(document.get("current_schema_hash"), str), "current_schema_hash must be a string"
    statements = document["statements"]
    assert statements is None or isinstance(statements, list), f"statements must be null or a list: {statements!r}"
    return [statement_from_json(entry, f"statement {i}") for i, entry in enumerate(statements or [])]


def _bind(actual, expected):
    """Find a consistent binding of actual temporary names to expected placeholders.

    Returns the actual statements rewritten with placeholders, or None.
    """
    tokens = []
    for statement in actual:
        for token in _TEMP_TOKEN.findall(statement["ddl"]):
            if token not in tokens:
                tokens.append(token)
    prefixes = {}
    for statement in expected:
        for match in _PLACEHOLDER.finditer(statement.ddl):
            start = statement.ddl.rfind("pgschemadiff_tmp", 0, match.start())
            prefixes.setdefault(match.group(1), statement.ddl[start : match.start()])
    if len(tokens) != len(prefixes):
        return None
    names = sorted(prefixes)
    for order in itertools.permutations(names):
        mapping = dict(zip(tokens, order))
        if not all(token.startswith(prefixes[name]) and len(token) > len(prefixes[name]) for token, name in mapping.items()):
            continue
        rewritten = []
        for statement in actual:
            ddl = norm(_TEMP_TOKEN.sub(lambda m: prefixes[mapping[m.group(0)]] + "{" + mapping[m.group(0)] + "}", statement["ddl"]))
            rewritten.append({**statement, "ddl": ddl})
        if Counter(_key(s) for s in rewritten) == Counter(s.key() for s in expected):
            return rewritten
    return None


def _key(statement):
    return (statement["ddl"], statement["timeout"], statement["lock"], statement["hazards"])


def _describe(statements):
    lines = []
    for i, s in enumerate(statements):
        if isinstance(s, S):
            lines.append(f"  [{i}] t={s.timeout} l={s.lock} hz={sorted(s.hazards)} {s.ddl}")
        else:
            lines.append(f"  [{i}] t={s['timeout']} l={s['lock']} hz={sorted(s['hazards'])} {s['ddl']}")
    return "\n".join(lines)


def assert_plan(actual, expected, order=()):
    """Compare the complete multiset of statements, then each dependency chain.

    ``order`` holds chains of indexes into ``expected``; each chain must appear in
    increasing plan position. ``expected`` statements must be distinct.
    """
    assert len({s.key() for s in expected}) == len(expected), "expected statements must be distinct"
    bound = _bind(actual, expected)
    assert bound is not None, f"plan statements differ\nactual:\n{_describe(actual)}\nexpected:\n{_describe(expected)}"
    position = {_key(s): i for i, s in enumerate(bound)}
    for chain in order:
        places = [position[expected[i].key()] for i in chain]
        assert places == sorted(places), (
            "dependency order violated for "
            + " -> ".join(expected[i].ddl for i in chain)
            + f"\nactual:\n{_describe(bound)}"
        )
    return bound


def plan(args, server=None, expect_ok=True):
    """Run `plan ... --output-format json`; returns statements (and checks temp databases are gone)."""
    before = server.databases() if server else None
    result = run_program(["plan", *args, "--output-format", "json"])
    if server:
        assert server.databases() == before, "temporary databases were left behind"
    if expect_ok:
        assert result.returncode == 0, f"plan failed ({result.returncode}): {result.stderr[-2000:]}"
        return decode_plan(result.stdout)
    return result


_SQL_BLOCK = re.compile(
    r"\A/\*\nStatement (\d+)\n((?:.*\n)*?)\*/\n"
    r"SET SESSION statement_timeout = (\d+);\n"
    r"SET SESSION lock_timeout = (\d+);\n"
    r"(.*);\Z",
    re.S,
)


def parse_sql_plan(stdout):
    """Parse the sql output format into (numbers, statements)."""
    text = stdout.rstrip("\n")
    if not text.strip():
        return [], []
    chunks = re.split(r"\n+(?=/\*\nStatement \d+\n)", text)
    numbers, statements = [], []
    for chunk in chunks:
        match = _SQL_BLOCK.match(chunk)
        assert match, f"unparseable sql plan block:\n{chunk[:800]}"
        numbers.append(int(match.group(1)))
        hazards = frozenset(re.findall(r"^\s*-\s*([A-Z_]+)", match.group(2), re.M))
        statements.append({"ddl": norm(match.group(5)), "timeout": int(match.group(3)), "lock": int(match.group(4)), "hazards": hazards})
    return numbers, statements


def split_dump(stdout):
    """Split dump output into statements (each terminated by ';' and separated by a blank line)."""
    parts = [p for p in re.split(r";\n\n", stdout.strip()) if p.strip()]
    return [norm(p) for p in parts]


def last_line(text):
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-1].strip() if lines else ""


CATALOG_QUERIES = {
    "columns": """
        select c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,
               pg_get_expr(d.adbin, d.adrelid)
        from pg_attribute a join pg_class c on c.oid = a.attrelid
        join pg_namespace n on n.oid = c.relnamespace
        left join pg_attrdef d on d.adrelid = a.attrelid and d.adnum = a.attnum
        where c.relkind = 'r' and a.attnum > 0 and not a.attisdropped
          and n.nspname not in ('pg_catalog', 'information_schema') and n.nspname not like 'pg_toast%'
    """,
    "indexes": """
        select n.nspname, i.indexrelid::regclass::text, pg_get_indexdef(i.indexrelid), i.indisvalid
        from pg_index i join pg_class c on c.oid = i.indexrelid join pg_namespace n on n.oid = c.relnamespace
        where n.nspname not in ('pg_catalog', 'information_schema') and n.nspname not like 'pg_toast%'
    """,
    "constraints": """
        select r.relname, co.conname, co.contype, pg_get_constraintdef(co.oid), co.convalidated
        from pg_constraint co join pg_class r on r.oid = co.conrelid join pg_namespace n on n.oid = r.relnamespace
        where n.nspname not in ('pg_catalog', 'information_schema')
    """,
    "sequences": """
        select s.seqrelid::regclass::text, format_type(s.seqtypid, null), s.seqstart, s.seqincrement, s.seqmin,
               s.seqmax, s.seqcache, s.seqcycle,
               (select d.refobjid::regclass::text || '.' || a.attname from pg_depend d
                  join pg_attribute a on a.attrelid = d.refobjid and a.attnum = d.refobjsubid
                  where d.objid = s.seqrelid and d.deptype in ('a', 'i') and d.classid = 'pg_class'::regclass limit 1)
        from pg_sequence s
    """,
    "enums": """
        select t.typname, string_agg(e.enumlabel, ',' order by e.enumsortorder)
        from pg_type t join pg_enum e on e.enumtypid = t.oid group by t.typname
    """,
    "functions": """
        select p.oid::regprocedure::text, pg_get_functiondef(p.oid)
        from pg_proc p join pg_namespace n on n.oid = p.pronamespace
        where n.nspname not in ('pg_catalog', 'information_schema') and p.prokind = 'f'
    """,
    "triggers": """
        select c.relname, t.tgname, pg_get_triggerdef(t.oid)
        from pg_trigger t join pg_class c on c.oid = t.tgrelid
        where not t.tgisinternal
    """,
}


def catalog(db):
    """Order-independent snapshot of the schema objects the task admits."""
    snapshot = {}
    for name, query in CATALOG_QUERIES.items():
        rows = db.query(query)
        snapshot[name] = Counter(tuple(row) for row in rows)
    return snapshot


def column_order(db, table):
    rows = db.query(
        "select attname from pg_attribute where attrelid = %s::regclass and attnum > 0 and not attisdropped order by attnum",
        (table,),
    )
    return [row[0] for row in rows]


def assert_same_catalog(actual, expected):
    for kind in CATALOG_QUERIES:
        missing = expected[kind] - actual[kind]
        extra = actual[kind] - expected[kind]
        assert not missing and not extra, f"{kind} differ\nmissing: {sorted(missing)}\nunexpected: {sorted(extra)}"


T20M = 1_200_000  # 20 minutes, in milliseconds


def plan_change(server, db, schema_dir, old, new, args=()):
    """Seed ``db`` with ``old`` DDL, write ``new`` as a schema dir and return the JSON plan."""
    if old:
        db.execute(old)
    files = new if isinstance(new, dict) else {"schema.sql": new}
    directory = schema_dir(files)
    return plan(["--from-dsn", db.dsn, "--to-dir", directory, *args], server)


def apply(server, source, target_args, *extra):
    """Run `apply --from-dsn SOURCE TARGET... --skip-confirm-prompt`; checks temp databases are gone."""
    before = server.databases()
    result = run_program(["apply", "--from-dsn", source.dsn, *target_args, "--skip-confirm-prompt", *extra], timeout=50)
    assert server.databases() == before, "temporary databases were left behind"
    return result


def rows(db, table):
    return sorted(db.query(f"select * from {table}"))
