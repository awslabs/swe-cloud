"""Key schema helpers.

A key schema is a handlebars-style template such as ``INFISICAL_{{secretKey}}`` or
``{{environment}}/{{secretKey}}``. Only the two variables below are supported.
"""

from __future__ import annotations

SECRET_KEY = "{{secretKey}}"
ENVIRONMENT = "{{environment}}"


class SchemaError(ValueError):
    pass


def render(schema: str, *, secret_key: str, environment: str) -> str:
    return schema.replace(ENVIRONMENT, environment).replace(SECRET_KEY, secret_key)


def key_with_schema(key: str, environment: str, schema: str | None) -> str:
    if not schema:
        return key
    return render(schema, secret_key=key, environment=environment)


def add_schema(source: dict[str, str], environment: str, schema: str | None) -> dict[str, str]:
    if not schema:
        return dict(source)
    return {key_with_schema(k, environment, schema): v for k, v in source.items()}


def matches_schema(key: str, environment: str, schema: str | None) -> bool:
    if not schema:
        return True

    pattern = render(schema, secret_key=SECRET_KEY, environment=environment)
    if SECRET_KEY not in pattern:
        return key == pattern

    parts = pattern.split(SECRET_KEY)
    prefix, suffix = parts[0], parts[-1]
    if prefix == "" and suffix == "":
        return True
    if prefix and not key.startswith(prefix):
        return False
    if suffix and not key.endswith(suffix):
        return False
    if len(key) < len(prefix) + len(suffix):
        return False

    portion = key[len(prefix): len(key) - len(suffix) if suffix else None]
    # With path separators in the schema, the secret key part must not contain one:
    # /path/segment/SECRET must not match /path/{{secretKey}}.
    if ("/" in prefix or "/" in suffix) and "/" in portion:
        return False
    return True


def full_path(path: str, schema: str | None, environment: str) -> str:
    """Parameter Store level that holds this sync's parameters.

    The path is prepended to the schema; a schema containing '/' moves the listing to a deeper
    level, e.g. path '/svc/' + schema '{{environment}}/{{secretKey}}' lists '/svc/dev/'.
    """
    if not schema or "/" not in schema:
        return path
    if schema.startswith("/"):
        raise SchemaError("key schema cannot start with '/'")

    segments = render(schema, secret_key=SECRET_KEY, environment=environment).split("/")
    dirs = segments[:-1]
    if any(SECRET_KEY in s for s in dirs):
        raise SchemaError("key schema cannot contain '/' after {{secretKey}}")
    return f"{path}{'/'.join(dirs)}/"
