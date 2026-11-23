"""KMS references: URL-safe ciphertext, encryption context, key/alg parameters, fragments."""

import base64
import json

import pytest

from checks import fails, ok, query, same, urlsafe


@pytest.fixture
def key(aws):
    meta = aws.key()
    return {"id": meta["KeyId"], "arn": meta["Arn"], "alias": aws.alias(meta["KeyId"])}


def padded_ciphertext(aws, key_id, base, context=None):
    """Encrypt `base` plus a suffix chosen so the URL-safe text ends with padding."""
    for extra in range(3):
        blob = aws.encrypt(key_id, (base + "." * extra).encode(), context)
        if urlsafe(blob).endswith("="):
            return (base + "." * extra), blob
    raise AssertionError("no padded ciphertext length found")


def test_decrypt_plain_ciphertext(aws, key, cli):
    plain, blob = padded_ciphertext(aws, key["id"], "hello-kms")
    ct = urlsafe(blob)
    assert ok(cli.get(f"ref+awskms://{ct}")).stdout == plain
    proc, docs = cli.eval(f"whole: ref+awskms://{ct}\nembedded: \"[ref+awskms://{ct}+]\"\n")
    ok(proc)
    same(docs, [{"whole": plain, "embedded": f"[{plain}]"}])


def test_ciphertext_encoding_must_be_padded_url_safe_base64(aws, key, cli):
    plain, blob = padded_ciphertext(aws, key["id"], "enc-check")
    fails(cli.get(f"ref+awskms://{urlsafe(blob).rstrip('=')}"))
    fails(cli.get("ref+awskms://not-base64!!"))
    # The standard alphabet's '+' ends the expression early.
    for attempt in range(40):
        blob = aws.encrypt(key["id"], f"std-{attempt}".encode())
        standard = base64.b64encode(blob).decode()
        if "+" in standard:
            fails(cli.get(f"ref+awskms://{standard}"))
            break
    else:
        pytest.fail("no ciphertext containing '+' was produced")


def test_encryption_context(aws, key, cli):
    context = {"app": "web", "env": "prod"}
    plain, blob = padded_ciphertext(aws, key["id"], "ctx-plain", context)
    ct = urlsafe(blob)
    for form in (json.dumps(context), json.dumps({"env": "prod", "app": "web"}), "{app: web, env: prod}"):
        assert ok(cli.get(f"ref+awskms://{ct}?context={query(form)}")).stdout == plain, form
    for wrong in (None, json.dumps({"app": "web"}), json.dumps({"app": "web", "env": "dev"})):
        expression = f"ref+awskms://{ct}" + (f"?context={query(wrong)}" if wrong else "")
        fails(cli.get(expression))
    # A context supplied for ciphertext encrypted without one also fails.
    _, bare = padded_ciphertext(aws, key["id"], "bare")
    fails(cli.get(f"ref+awskms://{urlsafe(bare)}?context={query(json.dumps(context))}"))


def test_key_and_algorithm_parameters(aws, key, cli):
    plain, blob = padded_ciphertext(aws, key["id"], "keyed")
    ct = urlsafe(blob)
    other = aws.key()["KeyId"]
    for params in (f"key={key['id']}", f"key={query(key['alias'])}", f"key={query(key['arn'])}",
                   "alg=SYMMETRIC_DEFAULT", "alg=NOT_AN_ALGORITHM", f"key={key['id']}&alg=SYMMETRIC_DEFAULT"):
        assert ok(cli.get(f"ref+awskms://{ct}?{params}")).stdout == plain, params
    fails(cli.get(f"ref+awskms://{ct}?key={other}"))


def test_fragment_into_decrypted_document(aws, key, cli):
    payload = json.dumps({"tok": "abc", "n": 3, "inner": {"z": False, "l": ["a", 1]}})
    context = {"app": "web"}
    blob = aws.encrypt(key["id"], payload.encode(), context)
    r = f"ref+awskms://{urlsafe(blob)}?context={query(json.dumps(context))}#"
    proc, docs = cli.eval(f"tok: {r}/tok\nn: {r}/n\nz: {r}/inner/z\ninner: {r}/inner\n")
    ok(proc)
    same(docs, [{"tok": "abc", "n": 3, "z": False, "inner": {"z": False, "l": ["a", 1]}}])
    assert ok(cli.get(f"{r}/inner/z")).stdout == "false"
    proc, _ = cli.eval(f"v: {r}/missing\n")
    fails(proc)
    _, text_blob = padded_ciphertext(aws, key["id"], "not-a-document")
    fails(cli.get(f"ref+awskms://{urlsafe(text_blob)}#/x"))
