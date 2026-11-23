"""Download response details: disposition, ranges, served content type and caching of one-shot files."""

from plikenv import solo


def shared(plik, name, data, **options):
    alice = solo(plik)
    up, f = alice.upload(name, data, **options)
    return f"/file/{up['id']}/{f['id']}/{name}"


def test_download_names_the_file_inline(plik):
    path = shared(plik, "report.txt", b"inline body\n")
    resp = plik.client().get(path)
    assert resp.headers["Content-Disposition"] == 'filename="report.txt"'


def test_download_parameter_makes_an_attachment(plik):
    path = shared(plik, "report.txt", b"attached body\n")
    resp = plik.client().get(path + "?dl=1")
    assert resp.status == 200 and resp.body == b"attached body\n"
    assert resp.headers["Content-Disposition"] == 'attachment; filename="report.txt"'


def test_range_request_returns_partial_content(plik):
    path = shared(plik, "digits.txt", b"0123456789abcdef")
    resp = plik.client().get(path, headers={"Range": "bytes=4-9"})
    assert resp.status == 206, resp
    assert resp.body == b"456789"
    assert resp.headers["Content-Length"] == "6"


def test_html_files_are_served_as_octet_stream(plik):
    alice = solo(plik)
    up, f = alice.upload("page.html", b"<html><body><p>hello</p></body></html>")
    assert f["fileType"] == "text/html; charset=utf-8"
    resp = plik.client().get(f"/file/{up['id']}/{f['id']}/page.html")
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "application/octet-stream"


def test_one_shot_download_disables_caching(plik):
    path = shared(plik, "once.txt", b"single use", oneShot=True)
    resp = plik.client().get(path)
    assert resp.status == 200 and resp.body == b"single use"
    assert resp.headers["Cache-Control"] == "no-cache, no-store, must-revalidate"


def test_regular_download_does_not_disable_caching(plik):
    path = shared(plik, "many.txt", b"reusable")
    resp = plik.client().get(path)
    assert resp.status == 200
    assert resp.headers.get("Cache-Control") is None
