"""Which inputs select a cache entry: compiler flags, preprocessed text, environment, compiler identity."""

from buildcache import expect_counters, lang_count, outcome
from harness import give_to_candidate, write_fixture

USES_HEADER = '#include "v.h"\nint v(void) { return V; }\n'

# Adapted from upstream tests/system.rs test_gcc_fprofile_generate_source_changes.
PROFILED = "/*line 1*/\n#ifndef UNDEFINED\n/*unused line 1*/\n#endif\n\nint main(int argc, char** argv) {\n  return 0;\n}\n"
PROFILED_SHIFTED = "/*line 1*/\n#ifndef UNDEFINED\n/*unused line 1*/\n/*unused line 2*/\n#endif\n\nint main(int argc, char** argv) {\n  return 0;\n}\n"


def header_project(cache):
    (cache.root / "inc").mkdir()
    (cache.root / "empty").mkdir()
    give_to_candidate(cache.root / "inc")
    give_to_candidate(cache.root / "empty")
    cache.write("inc/v.h", "#define V 11\n")
    cache.write("usev.c", USES_HEADER)


def test_compiler_flags_select_distinct_entries(cache):
    cache.start()
    header_project(cache)
    steps = [
        (("-Iinc", "-c", "usev.c", "-o", "a.o"), "miss"),
        (("-Iinc", "-O2", "-c", "usev.c", "-o", "b.o"), "miss"),
        (("-Iinc", "-c", "-O2", "usev.c", "-o", "c.o"), "hit"),
        (("-Iinc", "-c", "usev.c", "-O2", "-o", "d.o"), "hit"),
        (("-Iinc", "-DUNUSED_MACRO=1", "-c", "usev.c", "-o", "e.o"), "miss"),
        (("-Iinc", "-Wall", "-c", "usev.c", "-o", "f.o"), "miss"),
        (("-Iinc", "-std=c99", "-c", "usev.c", "-o", "g.o"), "miss"),
        (("-Iinc", "-g", "-c", "usev.c", "-o", "h.o"), "miss"),
        (("-Iinc", "-c", "usev.c", "-o", "i.o"), "hit"),
    ]
    assert [outcome(cache, *args) for args, _ in steps] == [expected for _, expected in steps]
    assert len(cache.entries()) == 6
    assert cache.read("a.o") == cache.read("i.o")
    assert cache.read("b.o") == cache.read("c.o") == cache.read("d.o")
    assert cache.read("a.o") != cache.read("b.o")


def test_search_paths_and_dependency_options_do_not_change_the_key(cache):
    cache.start()
    header_project(cache)
    steps = [
        (("-Iinc", "-c", "usev.c", "-o", "a.o"), "miss"),
        (("-Iinc", "-Iempty", "-c", "usev.c", "-o", "b.o"), "hit"),
        (("-I", "inc", "-c", "usev.c", "-o", "c.o"), "hit"),
        (("-Iinc", "-MD", "-MF", "usev.d", "-c", "usev.c", "-o", "d.o"), "hit"),
        (("-iquote", "inc", "-c", "usev.c", "-o", "e.o"), "hit"),
    ]
    assert [outcome(cache, *args) for args, _ in steps] == [expected for _, expected in steps]
    assert len(cache.entries()) == 1
    assert "inc/v.h" in (cache.root / "usev.d").read_text()


def test_key_follows_preprocessed_text(cache):
    cache.start()
    header_project(cache)
    first = outcome(cache, "-Iinc", "-c", "usev.c", "-o", "base.o")
    cache.write("usev.c", USES_HEADER.replace("return V;", "return V; /* reworded comment */"))
    comment = outcome(cache, "-Iinc", "-c", "usev.c", "-o", "comment.o")
    cache.write("usev.c", "\n\n" + USES_HEADER)
    blank = outcome(cache, "-Iinc", "-c", "usev.c", "-o", "blank.o")
    cache.write("inc/v.h", "#define V 12\n")
    header = outcome(cache, "-Iinc", "-c", "usev.c", "-o", "header.o")
    cache.write("inc/v.h", "#define V 11\n")
    reverted = outcome(cache, "-Iinc", "-c", "usev.c", "-o", "reverted.o")
    assert [first, comment, blank, header, reverted] == ["miss", "hit", "hit", "miss", "hit"]
    assert cache.read("reverted.o") == cache.read("base.o")
    assert cache.read("header.o") != cache.read("base.o")
    assert len(cache.entries()) == 2


def test_line_numbers_matter_only_for_profile_generation(cache):
    cache.start()
    cache.write("source.c", PROFILED)
    steps = [
        (PROFILED, ("-fprofile-generate",), "miss"),
        (PROFILED, ("-fprofile-generate",), "hit"),
        (PROFILED_SHIFTED, ("-fprofile-generate",), "miss"),
        (PROFILED_SHIFTED, (), "miss"),
        (PROFILED, (), "hit"),
    ]
    observed = []
    for index, (text, flags, _) in enumerate(steps):
        cache.write("source.c", text)
        observed.append(outcome(cache, "-c", "source.c", "-o", f"s{index}.o", *flags))
    assert observed == [expected for _, _, expected in steps]
    assert len(cache.entries()) == 3


def test_working_directory_and_input_spelling_share_an_entry(cache):
    cache.start()
    source = "int spelled(int x) { return x ^ 0x5a; }\n"
    cache.write("spelled.c", source)
    nested = cache.root / "nested"
    nested.mkdir()
    give_to_candidate(nested)
    write_fixture(nested / "spelled.c", source)
    steps = [
        ((("-c", "spelled.c", "-o", "a.o"), None), "miss"),
        ((("-c", "spelled.c", "-o", "b.o"), nested), "hit"),
        ((("-c", "nested/spelled.c", "-o", "c.o"), None), "hit"),
        ((("-c", str(nested / "spelled.c"), "-o", "d.o"), None), "hit"),
    ]
    assert [outcome(cache, *args, cwd=cwd) for (args, cwd), _ in steps] == [expected for _, expected in steps]
    assert (nested / "b.o").read_bytes() == cache.read("a.o") == cache.read("d.o")
    assert len(cache.entries()) == 1


def test_only_the_cache_buster_variable_enters_the_key(cache):
    cache.start()
    cache.write("env.c", "int env_value = 17;\n")
    args = ("-c", "env.c", "-o", "env.o")
    steps = [
        ({}, "miss"),
        ({"UNRELATED_SETTING": "1", "CFLAGS": "-O3"}, "hit"),
        ({"SCCACHE_C_CUSTOM_CACHE_BUSTER": "alpha"}, "miss"),
        ({"SCCACHE_C_CUSTOM_CACHE_BUSTER": "alpha", "UNRELATED_SETTING": "2"}, "hit"),
        ({"SCCACHE_C_CUSTOM_CACHE_BUSTER": "beta"}, "miss"),
        ({}, "hit"),
    ]
    assert [outcome(cache, *args, env=env) for env, _ in steps] == [expected for _, expected in steps]
    assert len(cache.entries()) == 3


def test_compiler_aliases_share_entries(cache):
    cache.start()
    cache.write("alias.c", "int alias(int x) { return x + 40; }\n")
    steps = [("gcc", "miss"), ("cc", "hit"), ("/usr/bin/gcc", "hit"), ("gcc-12", "hit")]
    observed = []
    for index, (compiler, _) in enumerate(steps):
        before = cache.stats()
        result = cache.run(compiler, "-c", "alias.c", "-o", f"alias{index}.o")
        assert result.returncode == 0, result.stderr
        after = cache.stats()
        observed.append("hit" if lang_count(after["cache_hits"]) > lang_count(before["cache_hits"]) else "miss")
    assert observed == [expected for _, expected in steps]
    assert len({cache.read(f"alias{index}.o") for index in range(len(steps))}) == 1
    expect_counters(cache.stats(), compile_requests=4, requests_executed=4, compilations=1, cache_writes=1, hits=3, misses=1)
    assert len(cache.entries()) == 1
