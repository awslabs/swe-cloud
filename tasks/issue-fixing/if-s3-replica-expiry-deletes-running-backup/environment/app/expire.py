"""Repository maintenance and full-count retention."""
import logging
from repository import Repository, entry

log = logging.getLogger(__name__)


def reconstruct(repo):
    info, etag = repo.load()
    labels = repo.labels()
    for label in list(info["current"]):
        if not repo.exists(f"backup/{label}/backup.manifest"):
            log.warning("backup %s missing manifest removed from backup.info", label)
            del info["current"][label]
    for label in labels:
        if label not in info["current"] and repo.exists(f"backup/{label}/backup.manifest"):
            log.warning("backup %s found in repository added to backup.info", label)
            info["current"][label] = entry(repo.manifest(label))
    return info, etag


def retain(repo, info, count):
    labels = sorted(info["current"], reverse=True)
    for label in labels[count:]:
        log.info("expire full backup %s", label)
        repo.delete(f"backup/{label}/backup.manifest")
        repo.delete(f"backup/{label}/backup.manifest.copy")
        del info["current"][label]


def sweep(repo, info):
    labels = sorted(repo.labels(), reverse=True)
    for label in labels:
        if label not in info["current"]:
            log.info("remove expired backup %s", label)
            repo.remove(label)


def expire(bucket, retention_full):
    repo = Repository(bucket)
    info, _ = reconstruct(repo)
    retain(repo, info, retention_full)
    repo.save(info)
    sweep(repo, info)
    log.info("expire complete")


def reconcile(bucket):
    """Rebuild the restore inventory after host replacement."""
    repo = Repository(bucket)
    info, _ = reconstruct(repo)
    repo.save(info)
    sweep(repo, info)
    log.info("reconciliation complete")
