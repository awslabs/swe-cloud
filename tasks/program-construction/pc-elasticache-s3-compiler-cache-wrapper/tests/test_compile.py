"""Single compiler invocations: storing, restoring, replaying and classifying requests."""

import concurrent.futures

import pytest
from buildcache import decode_entry, expect_counters, expect_levels, not_cached, number, wait_for
from harness import give_to_candidate, run_command, write_fixture

HELLO = '#include <stdio.h>\n#include "greet.h"\nint main(void) { puts(GREETING); return 0; }\n'
GREET = '#define GREETING "hello from the cache"\n'
WARN = "#include <stdio.h>\nint f(void) { int unused = 3; return 0; }\n"
NOTE = '#pragma message "compiling note.c"\nint note_value = 4;\n'
BROKEN = "x\n"  # upstream tests/test_err.c


def direct_gcc(cache, *args, cwd):
    return run_command(["gcc", *args], cwd=cwd, env={"HOME": str(cache.root / "home"), "TMPDIR": str(cache.root / "tmp")}, capture_output=True, text=True, timeout=30)


def test_cold_compile_stores_one_entry_in_each_level(cache):
    cache.start()
    cache.write("hello.c", HELLO)
    cache.write("greet.h", GREET)
    result = cache.gcc("-c", "hello.c", "-o", "hello.o")
    assert (result.stdout, result.stderr) == ("", "")
    direct = direct_gcc(cache, "-c", "hello.c", "-o", "direct.o", cwd=cache.root)
    assert direct.returncode == 0
    obj = cache.read("hello.o")
    assert obj == cache.read("direct.o")

    expect_counters(cache.stats(), compile_requests=1, requests_executed=1, compilations=1, cache_writes=1, misses=1)
    entries = cache.entries()
    assert len(entries) == 1
    (value, ttl), = entries.values()
    assert 0 < ttl <= 600
    objects = wait_for(lambda: cache.objects())
    assert len(objects) == 1
    members = decode_entry(value)
    assert obj in members.values(), "the entry must contain the object file"
    assert decode_entry(next(iter(objects.values()))) == members
    wait_for(lambda: number(cache.levels()[1]["writes"]) == 1)
    expect_levels(cache.stats(), ("L0 (redis)", {"misses": 1, "writes": 1}), ("L1 (s3)", {"misses": 1, "writes": 1}))


def test_warm_compile_restores_object_from_first_level(cache):
    cache.start()
    cache.write("hello.c", HELLO)
    cache.write("greet.h", GREET)
    cache.gcc("-c", "hello.c", "-o", "hello.o")
    original = cache.read("hello.o")
    wait_for(lambda: cache.objects())
    stored = {key: value for key, (value, _) in cache.entries().items()}
    objects = cache.objects()
    cache.remove("hello.o")

    result = cache.gcc("-c", "hello.c", "-o", "hello.o")
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert cache.read("hello.o") == original
    expect_counters(cache.stats(), compile_requests=2, requests_executed=2, compilations=1, cache_writes=1, hits=1, misses=1)
    wait_for(lambda: number(cache.levels()[1]["writes"]) == 1)
    expect_levels(cache.stats(), ("L0 (redis)", {"hits": 1, "misses": 1, "writes": 1}), ("L1 (s3)", {"misses": 1, "writes": 1}))
    assert {key: value for key, (value, _) in cache.entries().items()} == stored
    assert cache.objects() == objects


def test_output_location_is_not_part_of_the_key(cache):
    cache.start()
    cache.write("hello.c", HELLO)
    cache.write("greet.h", GREET)
    (cache.root / "out").mkdir()
    give_to_candidate(cache.root / "out")
    cache.gcc("-c", "hello.c", "-o", "first.o")
    cache.gcc("-c", "hello.c", "-o", "out/second.o")
    cache.gcc("-c", "hello.c", "-o", "/dev/null")
    cache.gcc("-c", "hello.c")
    original = cache.read("first.o")
    assert cache.read("out/second.o") == original
    assert cache.read("hello.o") == original
    expect_counters(cache.stats(), compile_requests=4, requests_executed=4, compilations=1, cache_writes=1, hits=3, misses=1)
    assert len(cache.entries()) == 1


def test_compiler_diagnostics_are_replayed_from_each_level(cache):
    cache.start()
    cache.write("warn.c", WARN)
    cache.write("note.c", NOTE)
    first = {}
    for name in ("warn", "note"):
        result = cache.gcc("-Wall", "-c", f"{name}.c", "-o", f"{name}.o")
        direct = direct_gcc(cache, "-Wall", "-c", f"{name}.c", "-o", f"direct-{name}.o", cwd=cache.root)
        assert (result.returncode, result.stdout, result.stderr) == (direct.returncode, direct.stdout, direct.stderr)
        assert cache.read(f"{name}.o") == cache.read(f"direct-{name}.o")
        first[name] = (result.stderr, cache.read(f"{name}.o"))
    assert "unused variable" in first["warn"][0]
    assert "compiling note.c" in first["note"][0]

    for name in ("warn", "note"):
        cache.remove(f"{name}.o")
        result = cache.gcc("-Wall", "-c", f"{name}.c", "-o", f"{name}.o")
        assert (result.returncode, result.stdout, result.stderr) == (0, "", first[name][0])
        assert cache.read(f"{name}.o") == first[name][1]
    expect_counters(cache.stats(), compile_requests=4, requests_executed=4, compilations=2, cache_writes=2, hits=2, misses=2)

    # A fresh server with only the S3 copies still restores the captured diagnostics.
    assert len(wait_for(lambda: len(cache.objects()) == 2 and cache.objects())) == 2
    cache.restart()
    cache.drop_entries()
    for name in ("warn", "note"):
        cache.remove(f"{name}.o")
        result = cache.gcc("-Wall", "-c", f"{name}.c", "-o", f"{name}.o")
        assert (result.returncode, result.stdout, result.stderr) == (0, "", first[name][0])
        assert cache.read(f"{name}.o") == first[name][1]
    expect_counters(cache.stats(), compile_requests=2, requests_executed=2, hits=2)
    assert number(cache.levels()[1]["hits"]) == 2


@pytest.mark.parametrize(
    ("source", "flags"),
    [(BROKEN, ()), (WARN, ("-Wall", "-Werror"))],
    ids=["syntax-error", "warning-as-error"],
)
def test_failed_compile_is_never_stored(cache, source, flags):
    cache.start()
    cache.write("bad.c", source)
    results = [cache.gcc(*flags, "-c", "bad.c", "-o", "bad.o", check=False) for _ in range(2)]
    direct = direct_gcc(cache, *flags, "-c", "bad.c", "-o", "bad.o", cwd=cache.root)
    for result in results:
        assert result.returncode == direct.returncode == 1
        assert (result.stdout, result.stderr) == (direct.stdout, direct.stderr)
        assert "error" in result.stderr
    assert not (cache.root / "bad.o").exists()
    expect_counters(cache.stats(), compile_requests=2, requests_executed=2, compile_fails=2)
    expect_levels(cache.stats(), ("L0 (redis)", {"misses": 2}), ("L1 (s3)", {"misses": 2}))
    assert cache.entries() == {}
    assert cache.objects() == {}


def test_split_dwarf_outputs_are_restored_together(cache):
    cache.start()
    cache.write("split.c", "int test(){ return 7; }\n")  # upstream test_split_dwarf_object_generate_output_dir_changes
    cache.gcc("-g", "-gsplit-dwarf", "-c", "split.c", "-o", "one.o")
    produced = {name: cache.read(name) for name in ("one.o", "one.dwo")}
    entry, = (value for value, _ in cache.entries().values())
    members = decode_entry(entry).values()
    assert all(content in members for content in produced.values())
    for name in produced:
        cache.remove(name)
    cache.gcc("-g", "-gsplit-dwarf", "-c", "split.c", "-o", "one.o")
    assert {name: cache.read(name) for name in produced} == produced
    # The split DWARF file name is embedded in the object, so another output name is a new entry.
    cache.gcc("-g", "-gsplit-dwarf", "-c", "split.c", "-o", "two.o")
    assert (cache.root / "two.dwo").exists()
    expect_counters(cache.stats(), compile_requests=3, requests_executed=3, compilations=2, cache_writes=2, hits=1, misses=2)
    assert len(cache.entries()) == 2


def test_dependency_file_is_written_on_a_hit(cache):
    cache.start()
    cache.write("hello.c", HELLO)
    cache.write("greet.h", GREET)
    reference = cache.root / "reference"
    reference.mkdir()
    give_to_candidate(reference)
    write_fixture(reference / "hello.c", HELLO)
    write_fixture(reference / "greet.h", GREET)
    assert direct_gcc(cache, "-MD", "-MF", "deps.d", "-c", "hello.c", "-o", "x.o", cwd=reference).returncode == 0
    expected = (reference / "deps.d").read_text()
    assert "x.o:" in expected and "greet.h" in expected

    cache.gcc("-c", "hello.c", "-o", "plain.o")
    cache.gcc("-MD", "-MF", "deps.d", "-c", "hello.c", "-o", "x.o")
    assert (cache.root / "deps.d").read_text() == expected
    assert cache.read("x.o") == cache.read("plain.o")
    cache.remove("deps.d")
    cache.gcc("-MD", "-MF", "deps.d", "-c", "hello.c", "-o", "x.o")
    assert (cache.root / "deps.d").read_text() == expected
    expect_counters(cache.stats(), compile_requests=3, requests_executed=3, compilations=1, cache_writes=1, hits=2, misses=1)


NON_CACHEABLE = {
    "preprocess-only": ("-E", ["-E", "hello.c", "-o", "hello.i"], None),
    "standard-input": ("-", ["-x", "c", "-c", "-", "-o", "stdin.o"], "int from_stdin = 5;\n"),
    "multiple-inputs": ("multiple input files", ["-c", "hello.c", "two.c"], None),
    "save-temps": ("-save-temps", ["-save-temps", "-c", "two.c", "-o", "temps.o"], None),
    "make-rules": ("-M", ["-M", "hello.c"], None),
    "no-line-markers": ("-P", ["-c", "two.c", "-o", "p.o", "-P"], None),
    "profile-use": ("-fprofile-use", ["-c", "two.c", "-o", "u.o", "-fprofile-use"], None),
    "unknown-language": ("unknown source language", ["-c", "notes.txt", "-o", "notes.o"], None),
    "no-input": ("no input file", ["-c"], None),
}


def snapshot(directory):
    found = {}
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            found[path.relative_to(directory).as_posix()] = path.read_bytes().replace(str(directory).encode(), b"<dir>")
    return found


@pytest.mark.parametrize("case", sorted(NON_CACHEABLE))
def test_non_cacheable_invocations_run_the_compiler(cache, case):
    reason, args, stdin = NON_CACHEABLE[case]
    cache.start()
    trees = {}
    for side in ("direct", "wrapped"):
        directory = cache.root / side
        directory.mkdir()
        give_to_candidate(directory)
        write_fixture(directory / "hello.c", HELLO)
        write_fixture(directory / "greet.h", GREET)
        write_fixture(directory / "two.c", "int two(int x) { return x * 2; }\n")
        write_fixture(directory / "notes.txt", "plain notes\n")
        trees[side] = directory
    direct = run_command(["gcc", *args], cwd=trees["direct"], input=stdin, capture_output=True, text=True, timeout=30,
                         env={"HOME": str(cache.root / "home"), "TMPDIR": str(cache.root / "tmp")})
    wrapped = cache.gcc(*args, cwd=trees["wrapped"], stdin=stdin, check=False)

    def normal(text, side):
        return text.replace(str(trees[side]), "<dir>")

    assert wrapped.returncode == direct.returncode
    assert normal(wrapped.stdout, "wrapped") == normal(direct.stdout, "direct")
    assert normal(wrapped.stderr, "wrapped") == normal(direct.stderr, "direct")
    assert snapshot(trees["wrapped"]) == snapshot(trees["direct"])
    stats = cache.stats()
    expect_counters(stats, compile_requests=1, requests_not_cacheable=1)
    assert not_cached(stats) == {reason: 1}
    expect_levels(stats, ("L0 (redis)", {}), ("L1 (s3)", {}))
    assert cache.entries() == {}
    assert cache.objects() == {}


NON_COMPILATION = {
    "link": ["two.c", "main.c", "-o", "app"],
    "assembly-only": ["-S", "two.c", "-o", "two.s"],
    "compiler-version": ["--version"],
}


@pytest.mark.parametrize("case", sorted(NON_COMPILATION))
def test_non_compilation_requests_pass_through(cache, case):
    args = NON_COMPILATION[case]
    cache.start()
    cache.write("two.c", "int two(int x) { return x * 2; }\n")
    cache.write("main.c", '#include <stdio.h>\nint two(int);\nint main(void) { printf("%d\\n", two(21)); return 0; }\n')
    direct = direct_gcc(cache, *args, cwd=cache.root)
    for produced in ("app", "two.s"):
        if (cache.root / produced).exists():
            cache.remove(produced)
    result = cache.gcc(*args)
    assert (result.returncode, result.stdout, result.stderr) == (direct.returncode, direct.stdout, direct.stderr)
    if case == "link":
        ran = run_command(["./app"], cwd=cache.root, capture_output=True, text=True, timeout=10)
        assert ran.stdout == "42\n"
    if case == "assembly-only":
        assert "two:" in cache.read("two.s").decode()
    expect_counters(cache.stats(), compile_requests=1, requests_not_compile=1)
    assert not_cached(cache.stats()) == {}
    assert cache.entries() == {}
    assert cache.objects() == {}


def test_unsupported_and_missing_compilers(cache):
    cache.start()
    cache.write("two.c", "int two(int x) { return x * 2; }\n")
    missing = cache.run("no-such-compiler-here", "-c", "two.c", "-o", "two.o")
    assert missing.returncode != 0
    expect_counters(cache.stats())
    unsupported = cache.run("/bin/true", "-c", "two.c", "-o", "two.o")
    assert unsupported.returncode != 0
    assert not (cache.root / "two.o").exists()
    expect_counters(cache.stats(), compile_requests=1, requests_unsupported_compiler=1)
    assert cache.entries() == {}


def test_concurrent_compiles_store_independent_entries(cache):
    cache.start()
    names = [f"unit{i}" for i in range(5)]
    for index, name in enumerate(names):
        cache.write(f"{name}.c", f"int {name}(int x) {{ return x * {index + 3} - {index}; }}\n")

    def build(name, output):
        return cache.gcc("-O1", "-c", f"{name}.c", "-o", output)

    with concurrent.futures.ThreadPoolExecutor(len(names)) as pool:
        list(pool.map(lambda name: build(name, f"{name}.o"), names))
    first = {name: cache.read(f"{name}.o") for name in names}
    assert len(set(first.values())) == len(names)
    assert len(cache.entries()) == len(names)
    assert len(wait_for(lambda: len(cache.objects()) == len(names) and cache.objects())) == len(names)
    with concurrent.futures.ThreadPoolExecutor(len(names)) as pool:
        list(pool.map(lambda name: build(name, f"{name}.again.o"), names))
    assert {name: cache.read(f"{name}.again.o") for name in names} == first
    expect_counters(cache.stats(), compile_requests=10, requests_executed=10, compilations=5, cache_writes=5, hits=5, misses=5)
