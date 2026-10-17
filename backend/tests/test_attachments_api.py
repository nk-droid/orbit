"""The routes the app uses for files that go out with an email."""

import json
from base64 import urlsafe_b64decode
from email import message_from_bytes, policy

import pytest

import attachments
from actions import store as action_log
from api import mail, settings
from drafting import store
from drafting.triage import Triage
from integrations.google.api import GoogleApiError

AUTH = "https://www.googleapis.com/auth/"
WRITE = {
    "account": "me@example.com",
    "scopes": [AUTH + "gmail.readonly", AUTH + "gmail.compose", AUTH + "gmail.modify"],
}
MESSAGE = "18f2a9c4d7e1b360"
REPLY = "Hi Asha,\n\nFriday works.\n\nNidhish"
PDF = b"%PDF-1.7 the budget"
PNG = b"\x89PNG\r\n\x1a\n a picture"
APP = {"X-Orbit-App": "1"}


_APP = None


@pytest.fixture(scope="module", autouse=True)
def the_api():
    """The whole API as it is served, without the LiveKit keys it reads on starting."""
    global _APP
    made_up = settings.Settings(
        livekit_url="wss://orbit.invalid",
        livekit_api_key="",
        livekit_api_secret="",
        allowed_origins=tuple(settings.DEFAULT_ORIGINS.split(",")),
    )
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(settings, "get_settings", lambda: made_up)
        from api.app import app
    _APP = app


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("ORBIT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(mail.tokens, "load", lambda: WRITE)


async def call(method: str, path: str, *, body: bytes = b"", headers=None, query=""):
    """One request to the API, as a browser would make it. Answers with the status,
    the headers and the body, which is read as JSON when it is JSON."""
    pieces = [body[i : i + 65536] for i in range(0, len(body), 65536)] or [b""]
    answered: list[dict] = []

    async def receive() -> dict:
        if not pieces:
            return {"type": "http.disconnect"}
        piece = pieces.pop(0)
        return {"type": "http.request", "body": piece, "more_body": bool(pieces)}

    async def send(message: dict) -> None:
        answered.append(message)

    given = {"content-length": str(len(body))}
    given.update({name.lower(): value for name, value in (headers or {}).items()})
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": query.encode(),
        "headers": [(k.encode(), v.encode()) for k, v in given.items()],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    await _APP(scope, receive, send)
    start, *rest = answered
    got = {k.decode(): v.decode() for k, v in start["headers"]}
    data = b"".join(part.get("body", b"") for part in rest)
    if got.get("content-type", "").startswith("application/json"):
        data = json.loads(data)
    return start["status"], got, data


def post_json(path: str, body: dict):
    return call(
        "POST",
        path,
        body=json.dumps(body).encode(),
        headers={**APP, "Content-Type": "application/json"},
    )


def upload(path: str, name: str, data: bytes, mime_type="application/octet-stream"):
    return call(
        "POST",
        path,
        body=data,
        headers={**APP, "Content-Type": mime_type},
        query=f"name={name}",
    )


# --- The tray ------------------------------------------------------------------


async def test_a_file_from_the_users_computer_waits_in_the_tray():
    status, _, added = await upload("/api/attachments", "Q3%20budget.pdf", PDF)

    assert status == 200
    assert added == {
        "id": added["id"],
        "name": "Q3 budget.pdf",
        "kind": "PDF",
        "view": "pdf",
        "size": "19 bytes",
        "source": "from your computer",
    }
    (kept,) = attachments.held_by(attachments.TRAY)
    assert attachments.read(kept) == PDF
    _, _, listed = await call("GET", "/api/attachments")
    assert listed == [added]


async def test_a_large_file_arrives_whole():
    big = bytes(range(256)) * 12_000  # 3 MB, sent in many pieces

    status, _, added = await upload("/api/attachments", "film.bin", big)

    assert (status, added["size"], added["view"]) == (200, "3.1 MB", "none")
    assert attachments.read(attachments.find(added["id"])) == big


async def test_only_the_app_itself_may_add_or_remove_a_file():
    kept = attachments.keep(attachments.TRAY, "a.pdf", "", PDF, "from your computer")

    added, _, _ = await call("POST", "/api/attachments", body=PDF, query="name=b.pdf")
    removed, _, _ = await call("DELETE", f"/api/attachments/{kept.id}")

    assert (added, removed) == (403, 403)
    assert attachments.held_by(attachments.TRAY) == [kept]


async def test_another_site_is_not_let_in_to_remove_a_file():
    status, headers, _ = await call(
        "OPTIONS",
        "/api/attachments/abc",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "DELETE",
            "Access-Control-Request-Headers": "x-orbit-app",
        },
    )

    assert status == 400
    assert "access-control-allow-origin" not in headers


async def test_the_app_itself_is_let_in_to_remove_a_file():
    status, headers, _ = await call(
        "OPTIONS",
        "/api/attachments/abc",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "DELETE",
            "Access-Control-Request-Headers": "x-orbit-app",
        },
    )

    assert status == 200
    assert "DELETE" in headers["access-control-allow-methods"]


async def test_more_than_an_email_can_carry_is_refused_before_it_is_read(monkeypatch):
    monkeypatch.setattr(attachments, "MAX_TOTAL_BYTES", 30)
    await upload("/api/attachments", "a.pdf", PDF)

    status, _, refused = await upload("/api/attachments", "b.pdf", PDF)

    assert status == 413
    assert "more than one email can carry" in refused["detail"]
    assert len(attachments.held_by(attachments.TRAY)) == 1


async def test_a_file_that_says_it_is_small_is_still_stopped_at_the_limit(monkeypatch):
    monkeypatch.setattr(attachments, "MAX_TOTAL_BYTES", 1000)

    status, _, _ = await call(
        "POST",
        "/api/attachments",
        body=b"x" * 200_000,
        headers={**APP, "Content-Length": "10"},
        query="name=big.bin",
    )

    assert status == 413
    assert attachments.held_by(attachments.TRAY) == []


async def test_an_empty_file_and_one_file_too_many_are_refused(monkeypatch):
    empty, _, _ = await upload("/api/attachments", "nothing.txt", b"")
    monkeypatch.setattr(attachments, "MAX_FILES", 1)
    await upload("/api/attachments", "a.pdf", PDF)

    extra, _, refused = await upload("/api/attachments", "b.pdf", PDF)

    assert (empty, extra) == (422, 413)
    assert "files at most" in refused["detail"]


async def test_a_file_is_taken_out_of_the_tray():
    _, _, added = await upload("/api/attachments", "a.pdf", PDF)

    status, _, _ = await call("DELETE", f"/api/attachments/{added['id']}", headers=APP)
    again, _, _ = await call("DELETE", f"/api/attachments/{added['id']}", headers=APP)

    assert (status, again) == (200, 404)
    assert attachments.held_by(attachments.TRAY) == []


async def test_a_file_on_a_draft_orbit_read_out_cannot_be_taken_off_it():
    kept = attachments.keep(attachments.draft("d1"), "a.pdf", "", PDF, "from Drive")

    status, _, refused = await call(
        "DELETE", f"/api/attachments/{kept.id}", headers=APP
    )

    assert status == 409
    assert "draft it again" in refused["detail"]
    assert attachments.find(kept.id) == kept


# --- Showing a file that is kept ----------------------------------------------


async def test_a_kept_picture_is_shown_as_a_picture_that_runs_nothing():
    kept = attachments.keep(
        attachments.TRAY, "logo.svg", "image/svg+xml", b"<svg/>", "x"
    )

    _, _, info = await call("GET", f"/api/files/attached/{kept.id}")
    status, headers, data = await call("GET", f"/api/files/attached/{kept.id}/content")

    assert (info["name"], info["kind"], info["view"]) == (
        "logo.svg",
        "picture",
        "image",
    )
    assert (status, data) == (200, b"<svg/>")
    assert headers["content-type"] == "image/svg+xml"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["content-security-policy"].startswith("sandbox")


async def test_a_kept_page_is_shown_clean_and_never_served_as_a_page():
    page = b"<h1>Plan</h1><script>steal()</script><p onclick='x()'>Book it.</p>"
    kept = attachments.keep(attachments.TRAY, "plan.html", "text/html", page, "x")

    _, _, info = await call("GET", f"/api/files/attached/{kept.id}")
    _, headers, _ = await call("GET", f"/api/files/attached/{kept.id}/content")

    assert info["view"] == "html"
    assert "<h1>Plan</h1>" in info["html"]
    assert "script" not in info["html"] and "onclick" not in info["html"]
    assert headers["content-type"] == "text/plain; charset=utf-8"


async def test_a_kept_file_of_a_kind_the_app_cannot_show_says_so():
    kept = attachments.keep(attachments.TRAY, "a.zip", "application/zip", b"PK", "x")

    _, _, info = await call("GET", f"/api/files/attached/{kept.id}")
    status, _, _ = await call("GET", f"/api/files/attached/{kept.id}/content")
    gone, _, _ = await call("GET", "/api/files/attached/nothing")

    assert (info["view"], status, gone) == ("none", 415, 404)


# --- Files on the reply waiting on an email's card -----------------------------


@pytest.fixture
def drafted():
    store.put(
        MESSAGE, Triage(gist="Asha asks about Friday.", needs_reply=True, reply=REPLY)
    )


@pytest.fixture
def gmail_api(monkeypatch) -> dict[str, list]:
    """Gmail is replaced by a recorder of what would be sent and what would be saved."""
    got: dict[str, list] = {"sent": [], "saved": []}

    async def reply_target(session, message_id):
        return {
            "to": ["asha@example.com"],
            "subject": "Re: Chapter 3",
            "thread_id": "thread-1",
            "in_reply_to": "<abc@mail.example.com>",
            "references": "<abc@mail.example.com>",
        }

    async def send(session, raw, thread_id=None):
        got["sent"].append((_email(raw), thread_id))
        return {"id": "sent-1", "thread_id": thread_id}

    async def save_draft(session, raw, thread_id=None):
        got["saved"].append((_email(raw), thread_id))
        return {"id": "r-1", "message_id": "18c0ffee"}

    async def answered(session, message_id):
        return False

    monkeypatch.setattr(mail.gmail, "answered", answered)
    monkeypatch.setattr(mail.gmail, "reply_target", reply_target)
    monkeypatch.setattr(mail.gmail, "send", send)
    monkeypatch.setattr(mail.gmail, "save_draft", save_draft)
    return got


def _email(raw: str):
    return message_from_bytes(urlsafe_b64decode(raw), policy=policy.default)


def _files(email) -> list[tuple[str, bytes]]:
    return [
        (p.get_filename(), p.get_content())
        for p in email.iter_parts()
        if p.get_filename()
    ]


DRAFT = f"/api/mail/{MESSAGE}/draft"


async def test_a_file_put_on_a_reply_goes_out_with_it(drafted, gmail_api):
    _, _, first = await upload(f"{DRAFT}/attachments", "Q3%20budget.pdf", PDF)
    status, _, both = await upload(
        f"{DRAFT}/attachments", "photo.png", PNG, "image/png"
    )
    assert status == 200
    assert [f["name"] for f in both] == ["Q3 budget.pdf", "photo.png"]
    assert first == both[:1]

    status, _, sent = await post_json(
        f"{DRAFT}/send", {"body": REPLY, "attachments": [f["id"] for f in both]}
    )

    assert (status, sent["status"], sent["attachments"]) == (200, "sent", [])
    ((email, thread_id),) = gmail_api["sent"]
    assert thread_id == "thread-1"
    assert email["In-Reply-To"] == "<abc@mail.example.com>"
    assert _files(email) == [("Q3 budget.pdf", PDF), ("photo.png", PNG)]
    # Sent, so the copies are not kept. The log still says what went.
    assert attachments.held_by(attachments.reply(MESSAGE)) == []
    (logged,) = action_log.recent()
    assert [f["name"] for f in logged["preview"]["attachments"]] == [
        "Q3 budget.pdf",
        "photo.png",
    ]


async def test_a_reply_goes_only_with_the_files_the_user_was_looking_at(
    drafted, gmail_api
):
    _, _, files = await upload(f"{DRAFT}/attachments", "a.pdf", PDF)

    without, _, refused = await post_json(f"{DRAFT}/send", {"body": REPLY})
    await upload(f"{DRAFT}/attachments", "b.pdf", PDF)
    stale, _, _ = await post_json(
        f"{DRAFT}/send", {"body": REPLY, "attachments": [files[0]["id"]]}
    )

    assert (without, stale) == (409, 409)
    assert "has changed" in refused["detail"]
    assert gmail_api["sent"] == []
    assert store.get(MESSAGE)["status"] == "suggested"
    assert len(attachments.held_by(attachments.reply(MESSAGE))) == 2


async def test_the_email_card_is_given_the_reply_with_its_files(drafted, monkeypatch):
    await upload(f"{DRAFT}/attachments", "a.pdf", PDF)

    async def raw_message(session, message_id):
        return {
            "id": MESSAGE,
            "threadId": "thread-1",
            "internalDate": "1790000000000",
            "snippet": "About Friday",
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "From", "value": "Asha <asha@example.com>"},
                    {"name": "To", "value": "me@example.com"},
                    {"name": "Subject", "value": "Chapter 3"},
                ],
                "body": {"data": "SGk"},
            },
        }

    monkeypatch.setattr(mail.gmail, "raw_message", raw_message)

    status, _, detail = await call("GET", f"/api/mail/{MESSAGE}")

    assert status == 200
    assert detail["draft"]["status"] == "suggested"
    assert [
        (f["name"], f["size"], f["source"]) for f in detail["draft"]["attachments"]
    ] == [("a.pdf", "19 bytes", "from your computer")]
    assert detail["draft"]["link"] is None


async def test_a_file_is_taken_off_a_reply(drafted, gmail_api):
    _, _, files = await upload(f"{DRAFT}/attachments", "a.pdf", PDF)

    await call("DELETE", f"/api/attachments/{files[0]['id']}", headers=APP)
    status, _, _ = await post_json(f"{DRAFT}/send", {"body": REPLY})

    assert status == 200
    ((email, _),) = gmail_api["sent"]
    assert _files(email) == [] and not email.is_multipart()


async def test_a_discarded_reply_lets_its_files_go(drafted):
    await upload(f"{DRAFT}/attachments", "a.pdf", PDF)

    status, _, _ = await post_json(f"{DRAFT}/discard", {})

    assert status == 200
    assert attachments.held_by(attachments.reply(MESSAGE)) == []


async def test_a_file_cannot_be_put_on_a_reply_that_is_no_longer_waiting(drafted):
    store.discard(MESSAGE)

    status, _, _ = await upload(f"{DRAFT}/attachments", "a.pdf", PDF)
    none, _, _ = await upload("/api/mail/unknown/draft/attachments", "a.pdf", PDF)

    assert (status, none) == (409, 404)
    assert attachments.held_by(attachments.reply(MESSAGE)) == []


async def test_a_reply_that_fails_to_send_keeps_its_files(
    drafted, gmail_api, monkeypatch
):
    async def send(session, raw, thread_id=None):
        raise GoogleApiError(500, "Backend error")

    monkeypatch.setattr(mail.gmail, "send", send)
    _, _, files = await upload(f"{DRAFT}/attachments", "a.pdf", PDF)

    status, _, _ = await post_json(
        f"{DRAFT}/send", {"body": REPLY, "attachments": [files[0]["id"]]}
    )

    assert status == 502
    assert store.get(MESSAGE)["status"] == "suggested"
    assert len(attachments.held_by(attachments.reply(MESSAGE))) == 1


# --- Finishing a reply in Gmail -----------------------------------------------


async def test_a_reply_is_put_in_the_gmail_drafts_with_its_files(drafted, gmail_api):
    _, _, files = await upload(f"{DRAFT}/attachments", "a.pdf", PDF)

    status, _, draft = await post_json(
        f"{DRAFT}/to-gmail", {"body": REPLY, "attachments": [files[0]["id"]]}
    )

    assert status == 200
    assert draft["status"] == "in_gmail"
    assert draft["link"] == (
        "https://mail.google.com/mail/?authuser=me%40example.com#drafts?compose=18c0ffee"
    )
    ((email, thread_id),) = gmail_api["saved"]
    assert thread_id == "thread-1"
    assert _files(email) == [("a.pdf", PDF)]
    assert email.get_body(("plain",)).get_content().strip() == REPLY
    # Nothing was sent, and it can no longer be sent from here.
    assert gmail_api["sent"] == []
    assert attachments.held_by(attachments.reply(MESSAGE)) == []
    again, _, refused = await post_json(f"{DRAFT}/send", {"body": REPLY})
    assert again == 409
    assert "Gmail drafts" in refused["detail"]


async def test_a_reply_in_gmail_no_longer_waits_on_the_user(drafted, gmail_api):
    await post_json(f"{DRAFT}/to-gmail", {"body": REPLY})

    assert not mail.owed_reply(store.get(MESSAGE))


async def test_a_reply_that_gmail_does_not_take_stays_where_it_was(
    drafted, gmail_api, monkeypatch
):
    async def save_draft(session, raw, thread_id=None):
        raise GoogleApiError(403, "Insufficient Permission")

    monkeypatch.setattr(mail.gmail, "save_draft", save_draft)
    _, _, files = await upload(f"{DRAFT}/attachments", "a.pdf", PDF)

    status, _, refused = await post_json(
        f"{DRAFT}/to-gmail", {"body": REPLY, "attachments": [files[0]["id"]]}
    )

    assert status == 502
    assert refused["detail"] == "Not saved. Insufficient Permission"
    assert store.get(MESSAGE)["status"] == "suggested"
    assert len(attachments.held_by(attachments.reply(MESSAGE))) == 1


async def test_the_app_is_told_where_a_draft_orbit_put_in_gmail_is():
    from actions.gate import ActionGate

    gate = ActionGate(on_change=action_log.save)
    action = gate.propose("gmail.send_email", {}, {"what": "Send new email"})
    gate.set_aside(action.id, {"gmail_draft": "https://mail.google.com/mail/#drafts"})
    other = gate.propose("gmail.send_email", {}, {"what": "Send new email"})

    _, _, listed = await call("GET", "/api/actions")

    by_id = {a["id"]: a for a in listed}
    assert by_id[action.id]["status"] == "cancelled"
    assert by_id[action.id]["gmail_draft"] == "https://mail.google.com/mail/#drafts"
    assert by_id[other.id]["gmail_draft"] is None
