"""Storage and API errors."""


class PathNotFoundError(Exception):
    def __init__(self, path):
        super().__init__(f"path not found: {path}")
        self.path = path


class BlobUnknown(Exception):
    pass


class TagUnknown(Exception):
    def __init__(self, tag):
        super().__init__(f"unknown tag={tag}")
        self.tag = tag


class RepositoryUnknown(Exception):
    def __init__(self, name):
        super().__init__(f"unknown repository name={name}")
        self.name = name


class APIError(Exception):
    """An error with an HTTP status and a registry error code."""

    def __init__(self, status, code, message, detail=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.detail = detail

    def body(self):
        err = {"code": self.code, "message": self.message}
        if self.detail is not None:
            err["detail"] = self.detail
        return {"errors": [err]}


def manifest_unknown(detail=None):
    return APIError(404, "MANIFEST_UNKNOWN", "manifest unknown", detail)


def manifest_invalid(detail=None):
    return APIError(400, "MANIFEST_INVALID", "manifest invalid", detail)


def digest_invalid(detail=None):
    return APIError(400, "DIGEST_INVALID", "provided digest did not match uploaded content", detail)


def name_unknown(name):
    return APIError(404, "NAME_UNKNOWN", "repository name not known to registry", {"name": name})


def unknown(detail=None):
    return APIError(500, "UNKNOWN", "unknown error", detail)
