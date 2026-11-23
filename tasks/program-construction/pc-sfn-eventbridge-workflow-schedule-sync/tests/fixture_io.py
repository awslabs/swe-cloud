"""Descriptor-relative mutations for verifier fixtures; ownership and modes stay explicit."""

import os
import shutil
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path


def open_directory(path, *, dir_fd=None):
    before = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode):
        raise OSError("Fixture directory is not an ordinary directory")
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
    try:
        opened = os.fstat(fd)
        after = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
        if not stat.S_ISDIR(after.st_mode) or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino) or (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino):
            raise OSError("Fixture directory changed while opening")
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def parent(path, *, root=None):
    path = Path(os.path.abspath(path))  # noqa: PTH100 -- do not resolve untrusted links
    temporary = Path(tempfile.gettempdir())
    roots = (Path(root),) if root is not None else (temporary, temporary.resolve(), Path("/app"), Path("/work"), Path("/home/candidate"))
    base = next((p for p in roots if p in path.parents or (p == path and p not in {temporary, temporary.resolve(), Path("/")})), None)
    if base is None:
        raise ValueError(f"Not a path within the declared fixture root: {path}")
    if root is None and base == temporary:
        path = temporary.resolve() / path.relative_to(temporary)
    fd = open_directory("/")
    try:
        for part in path.parent.parts[1:]:
            child = open_directory(part, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd, path.name
    finally:
        os.close(fd)


def open_file(path, mode="r", *args, root=None, **kwargs):
    if "opener" in kwargs or "closefd" in kwargs:
        raise ValueError("Fixture file descriptors are managed by the helper")
    with parent(path, root=root) as (fd, name):

        def opener(_path, flags):
            return os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o666, dir_fd=fd)

        return open(path, mode, *args, opener=opener, **kwargs)


def write_text(path, data, encoding=None, errors=None, newline=None, *, root=None):
    with open_file(path, "w", encoding=encoding, errors=errors, newline=newline, root=root) as stream:
        return stream.write(data)


def write_bytes(path, data, *, root=None):
    with open_file(path, "wb", root=root) as stream:
        return stream.write(data)


def chmod(path, mode, *, follow_symlinks=False, root=None):
    with parent(path, root=root) as (fd, name):
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            raise OSError("Refusing to change permissions through a fixture symlink")
        flags = getattr(os, "O_PATH", os.O_RDONLY | os.O_NONBLOCK) | os.O_NOFOLLOW
        target = os.open(name, flags, dir_fd=fd)
        try:
            if stat.S_ISLNK(os.fstat(target).st_mode):
                raise OSError("Fixture became a symlink")
            if hasattr(os, "O_PATH"):
                Path(f"/proc/self/fd/{target}").chmod(mode)
            else:
                os.fchmod(target, mode)
        finally:
            os.close(target)


def unlink(path, missing_ok=False, *, root=None):
    try:
        with parent(path, root=root) as (fd, name):
            os.unlink(name, dir_fd=fd)
    except FileNotFoundError:
        if not missing_ok:
            raise


def rmdir(path, *, root=None):
    with parent(path, root=root) as (fd, name):
        os.rmdir(name, dir_fd=fd)


def rmtree(path, ignore_errors=False, onerror=None, *, root=None):
    try:
        with parent(path, root=root) as (fd, name):
            shutil.rmtree(name, ignore_errors=ignore_errors, onerror=onerror, dir_fd=fd)
    except OSError:
        if not ignore_errors:
            raise


def mkdir(path, mode=0o777, parents=False, exist_ok=False, *, root=None):
    try:
        with parent(path, root=root) as (fd, name):
            try:
                os.mkdir(name, mode=mode, dir_fd=fd)
            except FileExistsError:
                if not exist_ok or not stat.S_ISDIR(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
                    raise
    except FileNotFoundError:
        if not parents:
            raise
        mkdir(Path(path).parent, parents=True, exist_ok=True, root=root)
        mkdir(path, mode=mode, exist_ok=exist_ok, root=root)


def mkfifo(path, mode=0o666, *, root=None):
    with parent(path, root=root) as (fd, name):
        os.mkfifo(name, mode, dir_fd=fd)


def copyfile(source, destination):
    # Inputs may be immutable test assets outside the writable fixture roots.
    with open_file(source, "rb", root="/") as incoming:
        with parent(destination) as (fd, name):
            target = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o666, dir_fd=fd)
        with os.fdopen(target, "wb") as outgoing:
            original, copied = os.fstat(incoming.fileno()), os.fstat(outgoing.fileno())
            if (original.st_dev, original.st_ino) == (copied.st_dev, copied.st_ino):
                raise shutil.SameFileError(source, destination)
            outgoing.truncate(0)
            shutil.copyfileobj(incoming, outgoing)
    return destination


def copy(source, destination):
    copyfile(source, destination)
    with open_file(source, "rb", root="/") as incoming:
        chmod(destination, stat.S_IMODE(os.fstat(incoming.fileno()).st_mode))
    return destination


def copy2(source, destination):
    copy(source, destination)
    with open_file(source, "rb", root="/") as incoming:
        info = os.fstat(incoming.fileno())
    utime(destination, ns=(info.st_atime_ns, info.st_mtime_ns))
    return destination


def copytree(source, destination, *, dirs_exist_ok=False):
    mkdir(destination, exist_ok=dirs_exist_ok)
    with parent(source, root="/") as (fd, name):
        directory = open_directory(name, dir_fd=fd)
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                src, dst = Path(source) / entry.name, Path(destination) / entry.name
                if entry.is_symlink():
                    raise OSError("Refusing to copy a linked fixture entry")
                if entry.is_dir(follow_symlinks=False):
                    copytree(src, dst, dirs_exist_ok=dirs_exist_ok)
                else:
                    copy2(src, dst)
        info = os.fstat(directory)
        chmod(destination, stat.S_IMODE(info.st_mode))
        utime(destination, ns=(info.st_atime_ns, info.st_mtime_ns))
    finally:
        os.close(directory)
    return destination


def utime(path, times=None, *, ns=None):
    with parent(path) as (fd, name):
        target = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    try:
        os.utime(target, ns=ns) if ns is not None else os.utime(target, times)
    finally:
        os.close(target)


def symlink(source, destination, target_is_directory=False):
    with parent(destination) as (fd, name):
        os.symlink(source, name, target_is_directory=target_is_directory, dir_fd=fd)


def link(source, destination):
    with parent(source) as (src_fd, src), parent(destination) as (dst_fd, dst):
        if stat.S_ISLNK(os.stat(src, dir_fd=src_fd, follow_symlinks=False).st_mode):
            raise OSError("Refusing to hard-link a fixture symlink")
        os.link(src, dst, src_dir_fd=src_fd, dst_dir_fd=dst_fd, follow_symlinks=False)


def rename(source, destination):
    with parent(source) as (src_fd, src), parent(destination) as (dst_fd, dst):
        os.rename(src, dst, src_dir_fd=src_fd, dst_dir_fd=dst_fd)
