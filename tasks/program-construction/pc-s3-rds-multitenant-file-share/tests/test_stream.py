"""Streaming uploads: bytes pass from a waiting uploader to one downloader and are never stored."""

import threading

from plikenv import error, solo, wait_until


def start_stream(alice, name, data):
    """Create a stream upload with one declared file and start sending its bytes in the background."""
    up = alice.create_upload(stream=True, files=[{"fileName": name}])
    file_id = up["files"][0]["id"]
    result = {}

    def send():
        result["resp"] = alice.api.add_file(up["id"], name, data, suffix=f"/{file_id}/{name}")

    sender = threading.Thread(target=send, daemon=True)
    sender.start()
    return up, file_id, sender, result


def receive(plik, path):
    """Poll the stream link until the waiting uploader is attached and the bytes arrive."""
    return wait_until(lambda: (r := plik.client().get(path)).status == 200 and r)


def test_stream_upload_hands_bytes_to_the_downloader(plik):
    alice = solo(plik)
    up, file_id, sender, _ = start_stream(alice, "live.txt", b"streamed through")
    resp = receive(plik, f"/stream/{up['id']}/{file_id}/live.txt")
    assert resp and resp.body == b"streamed through", resp
    sender.join(30)


def test_stream_upload_reply_reports_a_deleted_file(plik):
    alice = solo(plik)
    up, file_id, sender, result = start_stream(alice, "live.txt", b"streamed through")
    assert receive(plik, f"/stream/{up['id']}/{file_id}/live.txt")
    sender.join(30)
    body = result["resp"].json()
    assert result["resp"].status == 200
    assert body["id"] == file_id and body["status"] == "deleted" and body["fileSize"] == 16


def test_streamed_file_is_never_stored(plik):
    alice = solo(plik)
    up, file_id, sender, _ = start_stream(alice, "live.txt", b"streamed through")
    assert receive(plik, f"/stream/{up['id']}/{file_id}/live.txt")
    sender.join(30)
    assert plik.objects() == {}
    assert plik.sql("SELECT status, size FROM files") == [("deleted", 16)]


def test_streamed_file_cannot_be_downloaded_twice(plik):
    alice = solo(plik)
    up, file_id, sender, _ = start_stream(alice, "live.txt", b"streamed through")
    path = f"/stream/{up['id']}/{file_id}/live.txt"
    assert receive(plik, path)
    sender.join(30)
    error(plik.client().get(path), 404, f"file live.txt ({file_id}) is not available : deleted")


def test_stream_link_before_any_upload_is_not_found(plik):
    alice = solo(plik)
    up = alice.create_upload(stream=True, files=[{"fileName": "later.txt"}])
    file_id = up["files"][0]["id"]
    error(plik.client().get(f"/stream/{up['id']}/{file_id}/later.txt"), 404, f"file later.txt ({file_id}) is not available : missing")
