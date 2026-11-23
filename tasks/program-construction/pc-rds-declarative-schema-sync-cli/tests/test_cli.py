"""Invocation rules: required flags, refused combinations and failures that must leave databases untouched."""

from schema_sync import EMPTY_DATABASE, assert_catalog, errors, fails, ok


def setup(ctx, state="library.hcl"):
    target, dev = ctx.database(), ctx.database()
    ctx.fixture(state)
    return target, dev


def test_diff_with_desired_state_requires_dev_database(ctx):
    target, _ = setup(ctx)
    result = ctx.run("schema", "diff", "--from", ctx.url(target), "--to", "file://library.hcl")
    assert "--dev-url" in errors(result)
    assert ";" not in result.stdout
    assert_catalog(ctx, target, EMPTY_DATABASE)


def test_dry_run_and_auto_approve_are_exclusive(ctx):
    target, dev = setup(ctx)
    result = ctx.run("schema", "apply", "-u", ctx.url(target), "--to", "file://library.hcl", "--dev-url", ctx.url(dev),
                     "--dry-run", "--auto-approve")
    errors(result)
    assert ";" not in result.stdout
    assert_catalog(ctx, target, EMPTY_DATABASE)


def test_unsupported_apply_flags_fail_without_changes(ctx):
    target, dev = setup(ctx)
    base = ["schema", "apply", "-u", ctx.url(target), "--to", "file://library.hcl", "--dev-url", ctx.url(dev)]
    for extra in (["--edit"], ["--plan", "file://plan.txt"], ["--include", "public.authors"]):
        result = ctx.run(*base, *extra, "--auto-approve")
        fails(result)
        assert ";" not in result.stdout, extra
        assert_catalog(ctx, target, EMPTY_DATABASE)
    errors(ctx.run(*base, "--tx-mode", "bogus", "--auto-approve"))
    assert_catalog(ctx, target, EMPTY_DATABASE)


def test_apply_without_confirmation_does_not_change_the_database(ctx):
    target, dev = setup(ctx)
    result = ctx.run("schema", "apply", "-u", ctx.url(target), "--to", "file://library.hcl", "--dev-url", ctx.url(dev))
    fails(result)
    assert_catalog(ctx, target, EMPTY_DATABASE)
    assert_catalog(ctx, dev, EMPTY_DATABASE)


def test_invalid_desired_states_are_rejected(ctx):
    target, dev = setup(ctx)
    ctx.write("unclosed.hcl", 'table "x" {\n')
    ctx.write("unknown.hcl", 'table "x" {\n  schema = schema.nope\n  column "a" {\n    type = int\n  }\n}\n')
    for state in ("missing.hcl", "unclosed.hcl", "unknown.hcl"):
        args = ["-u", ctx.url(target), "--to", f"file://{state}", "--dev-url", ctx.url(dev)]
        result = ctx.run("schema", "apply", *args, "--auto-approve")
        errors(result)
        assert ";" not in result.stdout, state
        errors(ctx.run("schema", "diff", "--from", ctx.url(target), "--to", f"file://{state}", "--dev-url", ctx.url(dev)))
    assert_catalog(ctx, target, EMPTY_DATABASE)
    assert_catalog(ctx, dev, EMPTY_DATABASE)


def test_dev_database_must_be_clean(ctx):
    target, dev = setup(ctx)
    ctx.sql(dev, "CREATE TABLE junk (x int)")
    result = ctx.run("schema", "apply", "-u", ctx.url(target), "--to", "file://library.hcl", "--dev-url", ctx.url(dev),
                     "--auto-approve")
    errors(result)
    assert_catalog(ctx, target, EMPTY_DATABASE)
    assert ctx.sql(dev, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'") == [("junk",)]
    errors(ctx.run("schema", "diff", "--from", "file://library.hcl", "--to", "file://library.hcl", "--dev-url", ctx.url(dev)))


def test_connection_failures(ctx):
    target, _ = setup(ctx)
    server = ctx.server
    unreachable = f"postgres://{server.user}:x@{server.host}:1/{target}?sslmode=disable"
    errors(ctx.run("schema", "inspect", "-u", unreachable))
    assert "does not exist" in errors(ctx.run("schema", "inspect", "-u", ctx.url("no_such_database")))
    result = ctx.run("schema", "inspect", "-u", ctx.url(target, password="wrong-password"))
    assert "password authentication failed" in errors(result)
    assert ";" not in result.stdout
    # A successful inspection prints the state.
    assert ok(ctx.run("schema", "inspect", "-u", ctx.url(target))).strip()
