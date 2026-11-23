"""Primary-side full backup publication."""
import logging
from botocore.exceptions import ClientError
from repository import Repository, encode, entry

log = logging.getLogger(__name__)


def backup(bucket, label, files):
    repo = Repository(bucket)
    labels = repo.labels()
    if labels and label <= labels[-1]:
        raise ValueError("new backup label must sort after existing labels")
    prefix = f"backup/{label}/"
    manifest = {"label": label, "type": "full", "prior": None, "files": files}
    repo.put(prefix + "backup.manifest.copy", encode({**manifest, "copied": 0}))
    for index in range(files):
        repo.put(prefix + f"pg_data/{index:04d}", (f"{label}:{index}\n" * 128).encode())
        if (index + 1) % 4 == 0:
            repo.put(prefix + "backup.manifest.copy", encode({**manifest, "copied": index + 1}))
    body = encode(manifest)
    repo.put(prefix + "backup.manifest.copy", body)
    repo.put(prefix + "backup.manifest", body)
    for _ in range(32):
        info, etag = repo.load()
        info["current"][label] = entry(manifest)
        try:
            repo.save(info, etag)
            log.info("backup %s complete: %s files", label, files)
            return
        except ClientError as exc:
            if exc.response["ResponseMetadata"]["HTTPStatusCode"] != 412:
                raise
            repo.reconnect()
    raise RuntimeError("catalog publication retry limit")
