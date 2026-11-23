"""Storage layout. Mirrors the distribution v2 layout so existing tooling keeps working.

    docker/registry/v2/
        blobs/sha256/<hex[0:2]>/<hex>/data
        repositories/<name>/_manifests/
            revisions/sha256/<hex>/link
            tags/<tag>/current/link
            tags/<tag>/index/sha256/<hex>/link

Every link object holds a digest string ("sha256:<hex>").
"""

ROOT = "docker/registry/v2"


def _split(digest):
    alg, _, hex_ = digest.partition(":")
    return alg, hex_


def blob_data_path(digest):
    alg, hex_ = _split(digest)
    return f"{ROOT}/blobs/{alg}/{hex_[:2]}/{hex_}/data"


def manifests_root(name):
    return f"{ROOT}/repositories/{name}/_manifests"


def manifest_revision_link_path(name, digest):
    alg, hex_ = _split(digest)
    return f"{manifests_root(name)}/revisions/{alg}/{hex_}/link"


def manifest_tags_path(name):
    return f"{manifests_root(name)}/tags"


def manifest_tag_path(name, tag):
    return f"{manifest_tags_path(name)}/{tag}"


def manifest_tag_current_path(name, tag):
    return f"{manifest_tag_path(name, tag)}/current/link"


def manifest_tag_index_entry_link_path(name, tag, digest):
    alg, hex_ = _split(digest)
    return f"{manifest_tag_path(name, tag)}/index/{alg}/{hex_}/link"
