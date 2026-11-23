"""Device-style CLI login: a client starts a session, a logged-in user approves it, the client polls for a token."""

import pytest

from plikenv import error, number


@pytest.fixture
def started(open_pair, plik):
    alice, _ = open_pair
    resp = plik.client().post("/auth/cli/init", {"hostname": "build-box"})
    assert resp.status == 200, resp
    return alice, resp.json()


def poll(plik, session, secret=None):
    return plik.client().post("/auth/cli/poll", {"code": session["code"], "secret": secret or session["secret"]})


def test_started_session_is_pending(started, plik):
    _, session = started
    assert session["code"] and session["secret"]
    number(session["expiresIn"], 300)
    resp = poll(plik, session)
    assert resp.status == 200 and resp.json() == {"status": "pending"}, resp


def test_approval_requires_a_logged_in_user(started, plik):
    _, session = started
    resp = plik.client().post("/auth/cli/approve", {"code": session["code"]})
    assert resp.status == 401, resp
    assert poll(plik, session).json() == {"status": "pending"}


def test_approved_session_delivers_a_token_once(started, plik):
    alice, session = started
    resp = alice.session.post("/auth/cli/approve", {"code": session["code"], "comment": "build box"})
    assert resp.status == 200 and resp.json() == {"status": "approved"}, resp
    first = poll(plik, session)
    assert first.status == 200 and first.json()["status"] == "approved" and first.json()["token"], first
    error(poll(plik, session), 404, "CLI auth session not found")


def test_delivered_token_belongs_to_the_approving_user(started, plik):
    alice, session = started
    assert alice.session.post("/auth/cli/approve", {"code": session["code"], "comment": "build box"}).status == 200
    token = poll(plik, session).json()["token"]
    assert plik.sql("SELECT user_id, comment FROM tokens WHERE token = %s", token) == [(alice.id, "build box")]
    resp = plik.client(token).post("/upload", {})
    assert resp.status == 200, resp
    assert plik.sql('SELECT "user", token FROM uploads') == [(alice.id, token)]


def test_poll_with_a_wrong_secret_gets_no_token(started, plik):
    alice, session = started
    assert alice.session.post("/auth/cli/approve", {"code": session["code"]}).status == 200
    error(poll(plik, session, secret="00000000-0000-0000-0000-000000000000"), 401, "invalid secret")
    assert poll(plik, session).json()["status"] == "approved"


def test_approving_an_unknown_code_is_refused(started, plik):
    alice, _ = started
    error(alice.session.post("/auth/cli/approve", {"code": "ZZZZ-ZZZZ"}), 404, "CLI auth session not found or expired")


def test_session_cannot_be_approved_twice(started, plik):
    alice, session = started
    assert alice.session.post("/auth/cli/approve", {"code": session["code"]}).status == 200
    error(alice.session.post("/auth/cli/approve", {"code": session["code"]}), 400, "CLI auth session has already been approved")
    assert plik.sql("SELECT count(*) FROM tokens WHERE user_id = %s", alice.id) == [(2,)]
