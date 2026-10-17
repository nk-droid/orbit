import base64
import time
from email import message_from_bytes, policy
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from livekit.agents.llm import ToolError

import attachments
from actions.effects import AttachmentGoneError, clear_up, perform
from actions.gate import ActionGate
from agent_tools import actions, attaching, stage
from agent_tools.state import OrbitState
from integrations.google import api, gdrive, gmail
from integrations.google.api import GoogleApiError, TooLargeError

PDF = b"%PDF-1.7 the budget"
PNG = b"\x89PNG\r\n\x1a\n a picture"
DOC = "application/vnd.google-apps.document"
SHEET = "application/vnd.google-apps.spreadsheet"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
ASKED = "Email Asha and attach the Q3 budget from my Drive."


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """The store is a throwaway folder, and nobody waits for a transcript."""
    monkeypatch.setenv("ORBIT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(attaching, "HEARD_WITHIN", 0.0)


# --- The store ----------------------------------------------------------------


def test_a_file_is_kept_as_it_was_and_found_again():
    kept = attachments.keep(attachments.TRAY, "Q3 budget.pdf", "", PDF, "from Drive")

    assert attachments.find(kept.id) == kept
    assert attachments.read(kept) == PDF
    assert (kept.name, kept.mime_type, kept.size) == (
        "Q3 budget.pdf",
        "application/pdf",
        19,
    )
    assert attachments.held_by(attachments.TRAY) == [kept]
    assert attachments.held_by(attachments.draft("other")) == []


@pytest.mark.parametrize(
    ("given", "kept_as"),
    [
        ("../../etc/passwd", "etc passwd"),
        ("a\r\nBcc: someone@example.com.pdf", "a Bcc: someone@example.com.pdf"),
        ("C:\\Users\\me\\tax.pdf", "C: Users me tax.pdf"),
        ("", "attachment"),
        ("x" * 400 + ".pdf", "x" * 150),
    ],
)
def test_a_name_cannot_reach_outside_its_folder_or_its_header(given, kept_as):
    kept = attachments.keep(
        attachments.TRAY, given, "application/pdf", PDF, "from Drive"
    )

    assert kept.name == kept_as
    # It is stored under its own id, never under what it is called.
    assert attachments.read(kept) == PDF


def test_what_a_file_is_comes_from_its_name_when_nothing_says(monkeypatch):
    assert attachments.type_of("scan.JPG", "application/octet-stream") == "image/jpeg"
    assert attachments.type_of("notes.md", "") in ("text/markdown", "text/x-markdown")
    assert attachments.type_of("data.bin", "") == "application/octet-stream"
    assert attachments.type_of("scan.jpg", "image/png; x=1") == "image/png"


@pytest.mark.parametrize(
    ("name", "claimed", "kept_as"),
    [
        # Not types at all, one of them an attempt on the email's headers.
        ("notes.txt", "foo", "text/plain"),
        ("scan.pdf", "te xt/pl ain", "application/pdf"),
        ("x.bin", "x\r\nBcc: someone@example.com/y", "application/octet-stream"),
        ("x.bin", "text/", "application/octet-stream"),
        # An email cannot carry a file that says it is itself an email, or parts.
        ("note.eml", "message/rfc822", "application/octet-stream"),
        ("note.eml", "", "application/octet-stream"),
        ("odd.bin", "multipart/mixed", "application/octet-stream"),
    ],
)
def test_a_type_that_would_break_the_email_is_not_kept(name, claimed, kept_as):
    kept = attachments.keep(attachments.TRAY, name, claimed, b"From: x\r\n\r\nhi", "x")

    assert kept.mime_type == kept_as
    # Whatever it was kept as, an email can be built with it, and carries it whole.
    raw = gmail.build_raw(
        ["a@example.com"], "S", "B", attachments=[(kept.name, kept.mime_type, PDF)]
    )
    (carried,) = [p for p in _email(raw).iter_parts() if p.get_filename()]
    assert carried.get_payload(decode=True) == PDF
    assert _email(raw)["Bcc"] is None


def test_only_a_file_of_the_store_can_be_dropped(tmp_path):
    outside = tmp_path / "orbit.db"
    attachments.keep(attachments.TRAY, "a.pdf", "", PDF, "x")
    assert outside.exists()

    attachments.drop("../orbit.db")
    attachments.drop("")

    assert outside.exists()


def test_an_owner_cannot_hold_more_than_an_email_carries(monkeypatch):
    monkeypatch.setattr(attachments, "MAX_TOTAL_BYTES", 30)
    owner = attachments.draft("d1")
    attachments.keep(owner, "a.pdf", "", PDF, "from Drive")

    with pytest.raises(attachments.TooMuchError):
        attachments.keep(owner, "b.pdf", "", PDF, "from Drive")
    # Another owner's room is its own.
    attachments.keep(attachments.draft("d2"), "b.pdf", "", PDF, "from Drive")


def test_a_copy_is_a_second_file_and_dropping_one_leaves_the_other():
    first = attachments.keep(attachments.TRAY, "a.pdf", "", PDF, "from your computer")

    second = attachments.copy_to(first, attachments.draft("d1"))
    attachments.drop(first.id)

    assert attachments.find(first.id) is None
    assert attachments.read(second) == PDF
    assert second.source == "from your computer"


def test_what_is_left_behind_is_cleared_out_and_what_is_waiting_is_not(monkeypatch):
    old_draft = attachments.keep(
        attachments.draft("gone"), "a.pdf", "", PDF, "from Drive"
    )
    old_tray = attachments.keep(
        attachments.TRAY, "b.pdf", "", PDF, "from your computer"
    )
    old_reply = attachments.keep(
        attachments.reply("m-1"), "c.pdf", "", PDF, "from Drive"
    )
    two_days_on = time.time() + 2 * 24 * 60 * 60
    monkeypatch.setattr(attachments.time, "time", lambda: two_days_on)
    new_draft = attachments.keep(
        attachments.draft("now"), "d.pdf", "", PDF, "from Drive"
    )

    assert attachments.clear_out() == 2

    assert attachments.find(old_draft.id) is None
    assert attachments.find(old_tray.id) is None
    # A reply waits on its card far longer than a conversation lasts.
    assert attachments.find(old_reply.id) is not None
    assert attachments.find(new_draft.id) is not None

    # But a reply never sent does not keep its files for good.
    monkeypatch.setattr(attachments.time, "time", lambda: two_days_on + 31 * 86400)
    attachments.clear_out()
    assert attachments.find(old_reply.id) is None


@pytest.mark.parametrize(
    ("size", "said"), [(340, "340 bytes"), (340_000, "340 KB"), (1_240_000, "1.2 MB")]
)
def test_a_size_is_said_the_way_people_say_it(size, said):
    assert attachments.human_size(size) == said


# --- The email that carries them ----------------------------------------------


def _email(raw: str):
    return message_from_bytes(base64.urlsafe_b64decode(raw), policy=policy.default)


def test_an_email_carries_its_files_beside_its_words_and_its_formatting():
    raw = gmail.build_raw(
        ["asha@example.com"],
        "Budget",
        "Here it is.",
        html="<p>Here it <b>is</b>.</p>",
        attachments=[
            ("Q3 budget.pdf", "application/pdf", PDF),
            ("Übersicht №1.png", "image/png", PNG),
        ],
    )

    email = _email(raw)

    assert email.get_content_type() == "multipart/mixed"
    words, *carried = email.iter_parts()
    assert words.get_content_type() == "multipart/alternative"
    assert [p.get_content_type() for p in words.iter_parts()] == [
        "text/plain",
        "text/html",
    ]
    assert [
        (p.get_filename(), p.get_content_type(), p.get_content()) for p in carried
    ] == [
        ("Q3 budget.pdf", "application/pdf", PDF),
        ("Übersicht №1.png", "image/png", PNG),
    ]
    assert all(p.get_content_disposition() == "attachment" for p in carried)


def test_a_reply_with_a_file_stays_in_its_thread():
    raw = gmail.build_raw(
        ["asha@example.com"],
        "Re: Budget",
        "Attached.",
        attachments=[("b.pdf", "application/pdf", PDF)],
        in_reply_to="<m-1@mail>",
        references="<m-0@mail> <m-1@mail>",
    )

    email = _email(raw)

    assert email["In-Reply-To"] == "<m-1@mail>"
    assert email["References"] == "<m-0@mail> <m-1@mail>"
    assert email.get_content_type() == "multipart/mixed"


@pytest.fixture
def gmail_api(monkeypatch):
    """A stand-in for Gmail that keeps what was sent to it, and how."""
    received: list[dict] = []

    async def take(request: web.Request) -> web.Response:
        got: dict = {"path": request.path, "query": dict(request.query)}
        if request.content_type == "multipart/related":
            parts = await request.multipart()
            got["described"] = await (await parts.next()).json()
            message = await parts.next()
            got["type"] = message.headers["Content-Type"]
            got["message"] = bytes(await message.read())
        else:
            got["json"] = await request.json()
        received.append(got)
        return web.json_response(
            {"id": "sent-1", "threadId": "t-1", "message": {"id": "msg-1"}}
        )

    async def no_token() -> dict[str, str]:
        return {}

    monkeypatch.setattr(api, "_headers", no_token)
    return received, take


async def _with_gmail(gmail_api, monkeypatch, act):
    received, take = gmail_api
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.router.add_post("/{tail:.*}", take)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        monkeypatch.setattr(gmail, "_API", str(server.make_url("/gmail/v1/users/me")))
        monkeypatch.setattr(
            gmail, "_UPLOAD", str(server.make_url("/upload/gmail/v1/users/me"))
        )
        result = await act(session)
    return received, result


async def test_a_small_email_goes_the_ordinary_way(gmail_api, monkeypatch):
    raw = gmail.build_raw(
        ["asha@example.com"], "Budget", "Here.", attachments=[("b.pdf", "", PDF)]
    )

    (got,), sent = await _with_gmail(
        gmail_api, monkeypatch, lambda session: gmail.send(session, raw, "t-1")
    )

    assert got["path"] == "/gmail/v1/users/me/messages/send"
    assert got["json"] == {"raw": raw, "threadId": "t-1"}
    assert sent == {"id": "sent-1", "thread_id": "t-1"}


async def test_a_large_email_is_uploaded_as_the_message_it_is(gmail_api, monkeypatch):
    big = bytes(range(256)) * 20_000  # 5 MB, with every byte value in it
    raw = gmail.build_raw(
        ["asha@example.com"],
        "Film",
        "Here.",
        attachments=[("film.bin", "application/octet-stream", big)],
    )

    (got,), sent = await _with_gmail(
        gmail_api, monkeypatch, lambda session: gmail.send(session, raw, "t-1")
    )

    assert got["path"] == "/upload/gmail/v1/users/me/messages/send"
    assert got["query"] == {"uploadType": "multipart"}
    assert got["described"] == {"threadId": "t-1"}
    assert got["type"] == "message/rfc822"
    # The message itself, not text made of it: Gmail reads an upload as it is.
    (carried,) = [
        p
        for p in message_from_bytes(got["message"], policy=policy.default).iter_parts()
        if p.get_filename()
    ]
    assert carried.get_content() == big
    assert sent["id"] == "sent-1"


async def test_a_draft_saved_in_gmail_is_not_sent(gmail_api, monkeypatch):
    raw = gmail.build_raw(["asha@example.com"], "Budget", "Here.")

    (got,), saved = await _with_gmail(
        gmail_api, monkeypatch, lambda session: gmail.save_draft(session, raw, "t-1")
    )

    assert got["path"] == "/gmail/v1/users/me/drafts"
    assert got["json"] == {"message": {"raw": raw, "threadId": "t-1"}}
    assert saved == {"id": "sent-1", "message_id": "msg-1"}


async def test_a_large_draft_is_uploaded_to_the_drafts(gmail_api, monkeypatch):
    raw = gmail.build_raw(
        ["a@example.com"],
        "Film",
        "Here.",
        attachments=[("f.bin", "", b"x" * 4_000_000)],
    )

    (got,), _ = await _with_gmail(
        gmail_api, monkeypatch, lambda session: gmail.save_draft(session, raw)
    )

    assert got["path"] == "/upload/gmail/v1/users/me/drafts"
    assert got["described"] == {"message": {}}


# --- A Drive file as a file to send -------------------------------------------


@pytest.fixture
def drive(monkeypatch):
    """A stand-in for Drive holding one file. Records what was fetched from it."""
    held: dict = {
        "item": {
            "id": "f-1",
            "name": "Q3 budget",
            "mimeType": SHEET,
            "modifiedTime": "2026-10-01T09:30:00Z",
        },
        "bytes": PDF,
    }
    fetched: list[dict] = []

    async def get_json(session, url, params=None, **_):
        return held["item"]

    async def get_bytes(session, url, params=None, *, limit):
        fetched.append({"url": url.rsplit("/", 1)[-1], **(params or {})})
        if len(held["bytes"]) > limit:
            raise TooLargeError
        return held["bytes"]

    monkeypatch.setattr(api, "get_json", get_json)
    monkeypatch.setattr(api, "get_bytes", get_bytes)
    return held, fetched


async def test_a_google_sheet_is_sent_as_a_pdf_of_itself(drive):
    _, fetched = drive

    sent = await gdrive.to_attach(None, "f-1", office=False, limit=10**6)

    assert sent == ("Q3 budget.pdf", "application/pdf", PDF)
    assert fetched == [{"url": "export", "mimeType": "application/pdf"}]


async def test_a_google_sheet_is_sent_as_excel_when_that_is_asked_for(drive):
    _, fetched = drive

    name, mime_type, _ = await gdrive.to_attach(None, "f-1", office=True, limit=10**6)

    assert (name, mime_type) == ("Q3 budget.xlsx", XLSX)
    assert fetched == [{"url": "export", "mimeType": XLSX}]


async def test_a_file_with_bytes_of_its_own_is_sent_as_it_is(drive):
    held, fetched = drive
    held["item"] = {**held["item"], "name": "logo.png", "mimeType": "image/png"}
    held["bytes"] = PNG

    sent = await gdrive.to_attach(None, "f-1", office=True, limit=10**6)

    assert sent == ("logo.png", "image/png", PNG)
    assert fetched == [{"url": "f-1", "alt": "media"}]


async def test_a_file_too_large_is_not_even_fetched(drive):
    held, fetched = drive
    held["item"] = {**held["item"], "mimeType": "video/mp4", "size": "90000000"}

    with pytest.raises(TooLargeError):
        await gdrive.to_attach(None, "f-1", office=False, limit=25_000_000)
    assert fetched == []


@pytest.mark.parametrize(
    "mime_type",
    ["application/vnd.google-apps.folder", "application/vnd.google-apps.form"],
)
async def test_what_cannot_be_a_file_is_refused(drive, mime_type):
    held, _ = drive
    held["item"] = {**held["item"], "mimeType": mime_type}

    with pytest.raises(gdrive.UnreadableFileError):
        await gdrive.to_attach(None, "f-1", office=False, limit=10**6)


# --- Whether the user asked for a file ----------------------------------------


@pytest.mark.parametrize(
    ("said", "fetched", "asked"),
    [
        # A word of attaching asks, whatever the file is called.
        (ASKED, ["Q3 budget.pdf"], True),
        ("Yes, include the photo.", ["IMG_2041.jpeg"], True),
        ("Reply to Priya with the signed copy enclosed.", ["Terms.pdf"], True),
        # A word of sending asks when each file is named with it.
        ("Send Asha the budget.", ["Q3 budget.xlsx"], True),
        (
            "Email the invoice and the budget to Ravi.",
            ["Invoice Q3.pdf", "Q3 budget.pdf"],
            True,
        ),
        ("Send an email to Asha saying I\u2019m late.", ["passport.pdf"], False),
        ("Email the invoice to Ravi.", ["Invoice Q3.pdf", "passport.pdf"], False),
        # What kind of file it is says nothing about which file.
        ("Send the pdf.", ["passport.pdf"], False),
        # Words that ask for nothing to be sent with anything.
        ("What is my latest email?", ["passport.pdf"], False),
        ("Reply to her saying thanks.", ["Q3 budget.pdf"], False),
        # A file the user handed over themselves needs only to be sent.
        ("Send this to Asha.", [], True),
        ("Tell Asha I am on my way.", [], False),
    ],
)
def test_what_asks_for_a_file_to_go_with_an_email(said, fetched, asked):
    assert attaching.asks(said, fetched) is asked


@pytest.mark.parametrize(
    ("said", "asked"),
    [
        ("Forward Priya\u2019s email to Ravi.", True),
        ("Send it on to Ravi.", True),
        ("Share this with Ravi.", True),
        # Sending something is not passing this on.
        ("Send her a thank-you note.", False),
        ("Reply to her saying thanks.", False),
        ("What does Priya\u2019s email say?", False),
        # Nor is wanting a file attached to something else.
        ("Attach the budget to my reply.", False),
    ],
)
def test_what_asks_for_an_email_to_be_passed_on(said, asked):
    assert attaching.asks(said, ["Terms.pdf"], forwarding=True) is asked


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _context(*said: str, on_change=None):
    """A conversation in which the user has said these things."""
    clock = _Clock()
    gate = ActionGate(clock=clock, on_change=on_change)
    history = SimpleNamespace(
        items=[SimpleNamespace(role="user", text_content=text) for text in said]
    )
    context = SimpleNamespace(
        userdata=OrbitState(gate=gate),
        session=SimpleNamespace(history=history),
        disallow_interruptions=lambda: None,
    )
    return context, clock


def _says(context, who: str, text: str) -> None:
    """Something more is said, and reaches the record of the conversation."""
    context.session.history.items.append(SimpleNamespace(role=who, text_content=text))


TAX = ["tax.pdf"]


async def test_a_file_the_user_asked_for_is_let_through():
    context, _ = _context(ASKED)

    await attaching.asked_for(context, ["Q3 budget.pdf"], ["Q3 budget.pdf"])

    assert context.userdata.asked_about_attaching is None


async def test_words_that_put_a_file_on_one_draft_do_not_put_one_on_the_next():
    context, _ = _context(ASKED)
    await attaching.asked_for(context, ["Q3 budget.pdf"], ["Q3 budget.pdf"])
    _says(context, "assistant", "Sent. Anything else?")
    _says(context, "user", "Reply to Priya saying thanks.")

    with pytest.raises(ToolError, match="only when the user has asked"):
        await attaching.asked_for(context, ["passwords.pdf"], ["passwords.pdf"])

    # Asked for afresh, a file goes on again.
    _says(context, "user", "And attach the terms to it.")
    await attaching.asked_for(context, ["Terms.pdf"], ["Terms.pdf"])


async def test_a_file_nobody_asked_for_sends_the_model_to_ask_about_it_by_name():
    context, clock = _context("What is my latest email?")

    with pytest.raises(ToolError, match=r"whether they want tax\.pdf attached"):
        await attaching.asked_for(context, TAX, TAX)

    # When it was sent to ask, and how much had been said by then.
    assert context.userdata.asked_about_attaching == (clock.now, 1)


async def test_an_email_nobody_asked_to_pass_on_sends_the_model_to_ask():
    context, _ = _context("Send her a thank-you note.")

    with pytest.raises(ToolError, match=r"forwarded, with Terms\.pdf, sig\.png"):
        await attaching.asked_for(
            context, ["Terms.pdf", "sig.png"], ["Terms.pdf", "sig.png"], forwarding=True
        )


async def _sent_to_ask(*said: str):
    """A conversation in which the model tried to attach a file nobody asked for."""
    context, clock = _context(*said)
    with pytest.raises(ToolError):
        await attaching.asked_for(context, TAX, TAX)
    clock.now += 5
    return context, clock


async def test_asked_about_the_file_and_told_yes_one_draft_is_let_through():
    context, _ = await _sent_to_ask("Write to my accountant about the return.")
    _says(context, "assistant", "Do you want me to attach the tax file?")
    context.userdata.gate.note_user_input()
    _says(context, "user", "Yeah, go on.")

    await attaching.asked_for(context, TAX, TAX)

    # One answer is for one draft: the next file has to be asked for again.
    with pytest.raises(ToolError):
        await attaching.asked_for(context, TAX, TAX)


async def test_an_answer_that_was_heard_counts_before_it_reaches_the_record():
    context, _ = await _sent_to_ask("Write to my accountant about the return.")
    _says(context, "assistant", "Shall I attach the tax file to it?")
    context.userdata.gate.note_user_input()

    await attaching.asked_for(context, TAX, TAX)


@pytest.mark.parametrize(
    ("orbit_said", "user_said"),
    [
        # Orbit never spoke of attaching, so nothing the user said was about it.
        ("I have noted that. Anything else?", "No, that is all."),
        ("Shall I carry on?", "Yes."),
        # It asked, and was told no.
        ("Do you want me to attach the tax file?", "No."),
        ("Do you want me to attach the tax file?", "Don\u2019t, just write to him."),
        ("Shall I include the tax file?", "Never mind, cancel that."),
        # It asked, and has not been answered.
        ("Do you want me to attach the tax file?", None),
    ],
)
async def test_a_file_stays_off_unless_orbit_asked_about_it_and_was_not_told_no(
    orbit_said, user_said
):
    context, _ = await _sent_to_ask("What is in my inbox?")
    _says(context, "assistant", orbit_said)
    if user_said is not None:
        context.userdata.gate.note_user_input()
        _says(context, "user", user_said)

    with pytest.raises(ToolError, match="only when the user has asked"):
        await attaching.asked_for(context, TAX, TAX)


async def test_told_no_and_then_asked_for_another_file_that_one_goes_on():
    context, _ = await _sent_to_ask("What is in my inbox?")
    _says(context, "assistant", "Do you want me to attach the tax file?")
    _says(context, "user", "No, attach the receipts instead.")

    await attaching.asked_for(context, ["receipts.pdf"], ["receipts.pdf"])


# --- What the drafting tools make of the files the model names ----------------


@pytest.fixture
def allowed(monkeypatch):
    monkeypatch.setattr(actions, "_require_write", lambda service: None)
    monkeypatch.setattr(actions, "_http", lambda: None)


@pytest.fixture
def inbox(monkeypatch):
    """A stand-in for Gmail holding one email with two files."""
    message = {
        "id": "m-1",
        "threadId": "t-1",
        "internalDate": "1790000000000",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "From", "value": "Priya Raman <priya@example.com>"},
                {"name": "To", "value": "me@example.com"},
                {"name": "Subject", "value": "Renewal terms"},
                {"name": "Date", "value": "Sat, 03 Oct 2026 17:58:00 +0530"},
                {"name": "Message-ID", "value": "<m-1@mail>"},
            ],
            "parts": [
                {
                    "partId": "0",
                    "mimeType": "text/plain",
                    "filename": "",
                    "body": {
                        "data": _b64(b"Terms attached.\n\nPlease confirm by Friday.")
                    },
                },
                {
                    "partId": "1",
                    "mimeType": "application/pdf",
                    "filename": "Terms.pdf",
                    "body": {"attachmentId": "a-1", "size": len(PDF)},
                },
                {
                    "partId": "2",
                    "mimeType": "image/png",
                    "filename": "signature.png",
                    "body": {"data": _b64(PNG), "size": len(PNG)},
                },
            ],
        },
    }

    async def get_json(session, url, params=None, **_):
        if "/attachments/" in url:
            return {"data": _b64(PDF)}
        return message

    monkeypatch.setattr(api, "get_json", get_json)
    return message


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _drafted(context):
    (action,) = [
        a
        for a in context.userdata.gate._actions.values()
        if a.status == "awaiting_approval"
    ]
    return action


async def test_a_file_from_drive_goes_on_the_draft_as_a_copy(allowed, drive):
    context, _ = _context(ASKED)

    told = await actions.gmail_draft_email(
        context,
        ["asha@example.com"],
        "Budget",
        "Here it is.",
        "plain",
        ["drive:f-1"],
    )

    action = _drafted(context)
    (copy_id,) = action.payload["attachments"]
    copy = attachments.find(copy_id)
    # What the email will carry is the copy made now.
    assert (copy.name, copy.source, attachments.read(copy)) == (
        "Q3 budget.pdf",
        "from Drive",
        PDF,
    )
    assert action.preview["attachments"] == [
        {
            "id": copy_id,
            "name": "Q3 budget.pdf",
            "kind": "PDF",
            "view": "pdf",
            "size": "19 bytes",
            "source": "from Drive",
        }
    ]
    # The model is given names to say, and told to say them.
    assert told["draft"]["attachments"] == ["Q3 budget.pdf (19 bytes, from Drive)"]
    assert "1 attachment" in told["attached"] and "name each one" in told["attached"]


async def test_a_file_nobody_asked_for_is_not_drafted_and_not_kept(allowed, drive):
    context, _ = _context("Email Asha to say I am running late.")

    with pytest.raises(ToolError, match="only when the user has asked"):
        await actions.gmail_draft_email(
            context,
            ["asha@example.com"],
            "Late",
            "Running late.",
            "plain",
            ["drive:f-1"],
        )

    assert context.userdata.gate._actions == {}
    assert attachments.held_by(attachments.TRAY) == []
    assert _everything_kept() == []


def _everything_kept() -> list[str]:
    with attachments.connect(attachments._SCHEMA) as connection:
        return [
            row["name"] for row in connection.execute("SELECT name FROM attachments")
        ]


async def test_a_file_from_another_email_and_one_the_user_added(allowed, inbox):
    added = attachments.keep(
        attachments.TRAY, "photo.jpg", "image/jpeg", PNG, "from your computer"
    )
    context, _ = _context("Send Asha the terms and attach the photo I added.")

    await actions.gmail_draft_email(
        context,
        ["asha@example.com"],
        "Terms",
        "Both attached.",
        "plain",
        ["email:m-1/terms.pdf", "added:"],
    )

    action = _drafted(context)
    copies = [attachments.find(i) for i in action.payload["attachments"]]
    assert [(c.name, c.source) for c in copies] == [
        ("Terms.pdf", "from an email"),
        ("photo.jpg", "from your computer"),
    ]
    assert attachments.read(copies[0]) == PDF
    assert action.payload["from_tray"] == [added.id]
    # The file the user added is still theirs until the email has gone.
    assert attachments.find(added.id) is not None


@pytest.mark.parametrize(
    ("reference", "why"),
    [
        ("the budget", "Name each attachment"),
        ("added:", "Ask for it with ask_for_file"),
        ("email:m-1/nothing.zip", "It has: Terms.pdf, signature.png"),
    ],
)
async def test_a_file_that_cannot_be_had_is_explained(allowed, inbox, reference, why):
    context, _ = _context(ASKED)

    with pytest.raises(ToolError, match=why):
        await actions.gmail_draft_email(
            context,
            ["a@example.com"],
            "S",
            "B",
            "plain",
            [reference],
        )
    assert _everything_kept() == []


async def test_files_too_large_for_one_email_are_refused_together(
    allowed, drive, monkeypatch
):
    monkeypatch.setattr(attachments, "MAX_TOTAL_BYTES", 30)
    context, _ = _context(ASKED)

    with pytest.raises(ToolError, match="more than one email can carry"):
        await actions.gmail_draft_email(
            context,
            ["a@example.com"],
            "S",
            "B",
            "plain",
            ["drive:f-1", "drive:f-1"],
        )
    # Not even the one that would have fitted is kept.
    assert _everything_kept() == []


WORD = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _too_long_for(monkeypatch, *refused: str) -> list[str]:
    """Google refuses to export a document as these types. Says what was asked of it."""
    asked: list[str] = []

    async def get_bytes(session, url, params=None, *, limit):
        asked.append(params["mimeType"])
        if params["mimeType"] in refused:
            raise GoogleApiError(403, "This file is too large to be exported.")
        return b"PK the document"

    monkeypatch.setattr(api, "get_bytes", get_bytes)
    return asked


async def test_a_document_too_long_for_a_pdf_goes_as_a_word_file_and_orbit_says_so(
    allowed, drive, monkeypatch
):
    held, _ = drive
    held["item"] = {**held["item"], "name": "The long report", "mimeType": DOC}
    asked = _too_long_for(monkeypatch, "application/pdf")
    context, _ = _context(ASKED)

    told = await actions.gmail_draft_email(
        context, ["a@example.com"], "S", "B", "plain", ["drive:f-1"]
    )

    assert asked == ["application/pdf", WORD]
    (file,) = _drafted(context).preview["attachments"]
    assert file["name"] == "The long report.docx"
    assert "too long for Google to make a PDF of" in told["attached"]
    assert "The long report.docx" in told["attached"]


async def test_a_document_too_long_to_export_at_all_is_offered_as_a_link(
    allowed, drive, monkeypatch
):
    held, _ = drive
    held["item"] = {**held["item"], "mimeType": DOC}
    _too_long_for(monkeypatch, "application/pdf", WORD)
    context, _ = _context(ASKED)

    with pytest.raises(ToolError, match="offer to put its link"):
        await actions.gmail_draft_email(
            context, ["a@example.com"], "S", "B", "plain", ["drive:f-1"]
        )
    assert _everything_kept() == []


@pytest.mark.parametrize(
    ("mime_type", "called"),
    [
        ("application/vnd.google-apps.folder", "That is a folder,"),
        ("application/vnd.google-apps.form", "That is a kind of Google file,"),
    ],
)
async def test_what_cannot_be_sent_as_a_file_is_said_in_plain_words(
    allowed, drive, mime_type, called
):
    held, _ = drive
    held["item"] = {**held["item"], "mimeType": mime_type}
    context, _ = _context(ASKED)

    with pytest.raises(ToolError, match=called):
        await actions.gmail_draft_email(
            context, ["a@example.com"], "S", "B", "plain", ["drive:f-1"]
        )


@pytest.fixture
def sent(monkeypatch) -> list:
    """Whatever is sent through Gmail is kept here instead."""
    out: list = []

    async def send(session, raw, thread_id=None):
        out.append((_email(raw), thread_id))
        return {"id": "sent-1", "thread_id": thread_id}

    monkeypatch.setattr(gmail, "send", send)
    return out


def _agreed(context, clock) -> None:
    """The draft was read out and the user has said yes."""
    gate = context.userdata.gate
    clock.now += 1
    gate.note_agent_spoke(clock.now)
    clock.now += 1
    gate.note_user_input()


async def test_what_was_agreed_to_is_what_goes_out_and_then_the_copies_go(
    allowed, drive, sent
):
    held, _ = drive
    added = attachments.keep(
        attachments.TRAY, "photo.jpg", "image/jpeg", PNG, "from your computer"
    )
    context, clock = _context(ASKED, on_change=clear_up)
    told = await actions.gmail_draft_email(
        context,
        ["asha@example.com"],
        "Budget",
        "Here it is.",
        "plain",
        ["drive:f-1", "added:photo.jpg"],
    )
    copy_ids = list(_drafted(context).payload["attachments"])
    # The file in Drive changes after the draft was read out.
    held["bytes"] = b"%PDF-1.7 something else entirely"
    _agreed(context, clock)

    done = await actions.confirm_action(context, told["action_id"])

    ((email, _),) = sent
    carried = [p for p in email.iter_parts() if p.get_filename()]
    assert [(p.get_filename(), p.get_content()) for p in carried] == [
        ("Q3 budget.pdf", PDF),
        ("photo.jpg", PNG),
    ]
    assert done["action"]["attachments"] == [
        "Q3 budget.pdf (19 bytes, from Drive)",
        "photo.jpg (18 bytes, from your computer)",
    ]
    # Sent, so neither the copies nor the file the user handed over are kept.
    assert [attachments.find(i) for i in copy_ids] == [None, None]
    assert attachments.find(added.id) is None


async def test_a_draft_that_is_withdrawn_lets_its_copies_go_but_not_the_users_file(
    allowed, drive
):
    added = attachments.keep(
        attachments.TRAY, "photo.jpg", "image/jpeg", PNG, "from your computer"
    )
    context, _ = _context(ASKED, on_change=clear_up)
    told = await actions.gmail_draft_email(
        context,
        ["a@example.com"],
        "S",
        "B",
        "plain",
        ["added:photo"],
    )
    (copy_id,) = _drafted(context).payload["attachments"]

    await actions.cancel_action(context, told["action_id"])

    assert attachments.find(copy_id) is None
    assert attachments.find(added.id) is not None


async def test_a_new_draft_takes_the_place_of_the_old_one_and_of_its_copies(
    allowed, drive
):
    context, _ = _context(ASKED, on_change=clear_up)
    draft = (
        ["a@example.com"],
        "S",
        "B",
        "plain",
        ["drive:f-1"],
    )
    await actions.gmail_draft_email(context, *draft)
    (first,) = _drafted(context).payload["attachments"]

    await actions.gmail_draft_email(context, *draft)

    (second,) = _drafted(context).payload["attachments"]
    assert attachments.find(first) is None
    assert attachments.find(second) is not None


async def test_an_email_whose_file_has_gone_is_not_sent_without_it(sent):
    payload = {
        "to": ["a@example.com"],
        "subject": "S",
        "body": "B",
        "attachments": ["gone"],
    }

    with pytest.raises(AttachmentGoneError):
        await perform(None, "gmail.send_email", payload)
    assert sent == []


@pytest.mark.parametrize(
    ("said", "sent_as"),
    [
        ("Email Asha and attach the Q3 budget from my Drive.", "Q3 budget.pdf"),
        ("Email Asha the Q3 budget, attached as an Excel file.", "Q3 budget.xlsx"),
        ("Attach the budget so she can edit it: an editable copy.", "Q3 budget.xlsx"),
    ],
)
async def test_a_sheet_goes_as_a_pdf_unless_the_user_asks_for_excel(
    allowed, drive, said, sent_as
):
    context, _ = _context(said)

    await actions.gmail_draft_email(
        context, ["a@example.com"], "S", "B", "plain", ["drive:f-1"]
    )

    assert [f["name"] for f in _drafted(context).preview["attachments"]] == [sent_as]


async def test_a_draft_that_is_changed_keeps_the_kind_of_file_it_had(allowed, drive):
    context, _ = _context("Email Asha the Q3 budget, attached as an Excel file.")
    draft = ["a@example.com"], "S", "B", "plain", ["drive:f-1"]
    await actions.gmail_draft_email(context, *draft)
    _says(context, "user", "Make the subject shorter.")

    await actions.gmail_draft_email(context, *draft)

    # Still Excel, and still there without being asked for again.
    assert _files(context) == ["Q3 budget.xlsx"]

    # Until they ask for the other kind.
    _says(context, "user", "Actually attach it as a PDF.")
    await actions.gmail_draft_email(context, *draft)

    assert _files(context) == ["Q3 budget.pdf"]


async def test_excel_asked_for_once_is_not_what_every_later_sheet_goes_as(
    allowed, drive
):
    context, _ = _context(
        "Email Asha the Q3 budget, attached as an Excel file.", on_change=clear_up
    )
    draft = ["a@example.com"], "S", "B", "plain", ["drive:f-1"]
    told = await actions.gmail_draft_email(context, *draft)
    await actions.cancel_action(context, told["action_id"])
    _says(context, "user", "Now email Ravi and attach the Q3 budget.")

    await actions.gmail_draft_email(
        context, ["r@example.com"], "S", "B", "plain", ["drive:f-1"]
    )

    assert _files(context) == ["Q3 budget.pdf"]


async def test_a_reply_can_carry_a_file(allowed, drive, monkeypatch):
    async def reply_target(session, message_id):
        return {
            "to": ["priya@example.com"],
            "subject": "Re: Renewal terms",
            "thread_id": "t-1",
            "in_reply_to": "<m-1@mail>",
            "references": "<m-1@mail>",
        }

    monkeypatch.setattr(gmail, "reply_target", reply_target)
    context, _ = _context("Reply to Priya and attach the Q3 budget as an Excel file.")

    await actions.gmail_draft_reply(
        context,
        "m-1",
        "Attached.",
        "plain",
        ["drive:f-1"],
    )

    action = _drafted(context)
    assert action.kind == "gmail.send_reply"
    assert action.payload["thread_id"] == "t-1"
    (copy,) = [attachments.find(i) for i in action.payload["attachments"]]
    assert (copy.name, copy.mime_type) == ("Q3 budget.xlsx", XLSX)


# --- Passing an email on -------------------------------------------------------


async def test_a_forwarded_email_goes_whole_with_every_file_it_had(
    allowed, inbox, sent
):
    context, clock = _context(
        "Forward Priya's email about the renewal to Ravi.", on_change=clear_up
    )

    told = await actions.gmail_draft_forward(
        context,
        "m-1",
        ["ravi@example.com"],
        "For your records.",
        "plain",
    )

    action = _drafted(context)
    assert action.kind == "gmail.send_email"
    assert action.preview["what"] == "Forward email"
    assert action.payload["subject"] == "Fwd: Renewal terms"
    assert action.payload["body"] == (
        "For your records.\n\n---------- Forwarded message ----------\n"
        "From: Priya Raman <priya@example.com>\nDate: Sat, 03 Oct 2026 17:58:00 +0530\n"
        "Subject: Renewal terms\nTo: me@example.com\n\n"
        "Terms attached.\n\nPlease confirm by Friday."
    )
    assert told["draft"]["attachments"] == [
        "Terms.pdf (19 bytes, from the email)",
        "signature.png (18 bytes, from the email)",
    ]
    _agreed(context, clock)
    await actions.confirm_action(context, told["action_id"])
    ((email, _),) = sent
    assert [
        (p.get_filename(), p.get_content())
        for p in email.iter_parts()
        if p.get_filename()
    ] == [
        ("Terms.pdf", PDF),
        ("signature.png", PNG),
    ]


async def test_a_forward_with_a_formatted_note_quotes_the_email_under_it(
    allowed, inbox
):
    context, _ = _context("Forward Priya's email to Ravi.")

    await actions.gmail_draft_forward(
        context,
        "m-1",
        ["ravi@example.com"],
        "Please **check** this.",
        "markdown",
    )

    html = _drafted(context).payload["html"]
    assert html.startswith("<p>Please <strong>check</strong> this.</p>")
    assert (
        "---------- Forwarded message ----------<br>From: Priya Raman &lt;priya@example.com&gt;"
        in html
    )
    assert html.endswith("<p>Terms attached.</p><p>Please confirm by Friday.</p>")


async def test_an_email_is_not_passed_on_with_its_files_unless_the_user_asked(
    allowed, inbox
):
    context, _ = _context("What does Priya's email say?")

    with pytest.raises(ToolError, match="only when the user has asked"):
        await actions.gmail_draft_forward(
            context,
            "m-1",
            ["stranger@example.net"],
            "",
            "plain",
        )
    assert context.userdata.gate._actions == {}
    assert _everything_kept() == []


# --- A file Orbit writes -------------------------------------------------------


@pytest.fixture
def shown(monkeypatch) -> list[dict]:
    cards: list[dict] = []

    async def show(card, *, push=None):
        cards.append(card)

    monkeypatch.setattr(stage, "show", show)
    return cards


async def test_a_file_orbit_writes_waits_with_the_files_the_user_added(shown):
    context, _ = _context("Write up the plan as a file and attach it.")

    written = await attaching.write_attachment(
        context, "Trip plan", "# Plan\\n\\n- book the train", "markdown"
    )

    (kept,) = attachments.held_by(attachments.TRAY)
    assert (kept.name, kept.source) == ("Trip plan.md", "written by Orbit")
    assert attachments.read(kept) == b"# Plan\n\n- book the train"
    assert written == {
        "name": "Trip plan.md",
        "size": "24 bytes",
        "attach_as": "added:Trip plan.md",
    }
    # The user is shown where it is waiting.
    assert shown == [{"id": "tray", "kind": "tray"}]


async def test_a_written_file_keeps_the_kind_its_name_gives_it(shown):
    context, _ = _context()

    await attaching.write_attachment(
        context, "figures.csv", "item,cost\ntrain,900", "plain"
    )
    await attaching.write_attachment(
        context, "page", "<h1>Hi</h1><script>steal()</script>", "html"
    )

    figures, page = attachments.held_by(attachments.TRAY)
    assert (figures.name, figures.mime_type) == ("figures.csv", "text/csv")
    assert page.name == "page.html"
    assert b"<h1>Hi</h1>" in attachments.read(
        page
    ) and b"steal" not in attachments.read(page)


async def test_a_written_file_can_then_be_attached_by_its_name(allowed, shown):
    context, _ = _context("Write the plan as a file and attach it to an email to Asha.")
    written = await attaching.write_attachment(
        context, "Trip plan", "Book the train.", "plain"
    )

    await actions.gmail_draft_email(
        context,
        ["asha@example.com"],
        "Plan",
        "Attached.",
        "plain",
        [written["attach_as"]],
    )

    (copy,) = [attachments.find(i) for i in _drafted(context).payload["attachments"]]
    assert (copy.name, copy.source) == ("Trip plan.txt", "written by Orbit")


async def test_a_drive_file_too_large_to_send_is_offered_as_a_link(allowed, drive):
    held, _ = drive
    held["item"] = {**held["item"], "mimeType": "video/mp4", "size": "90000000"}
    context, _ = _context(ASKED)

    with pytest.raises(ToolError, match="more than one email can carry"):
        await actions.gmail_draft_email(
            context,
            ["a@example.com"],
            "S",
            "B",
            "plain",
            ["drive:f-1"],
        )
    assert _everything_kept() == []


# --- Putting a draft in Gmail instead of sending it ---------------------------


@pytest.fixture
def gmail_drafts(monkeypatch) -> list:
    """Whatever is saved in the Gmail drafts is kept here instead."""
    saved: list = []

    async def save_draft(session, raw, thread_id=None):
        saved.append((_email(raw), thread_id))
        return {"id": "r-1", "message_id": "18c0ffee"}

    monkeypatch.setattr(gmail, "save_draft", save_draft)
    monkeypatch.setattr(actions.tokens, "load", lambda: {"account": "me@example.com"})
    return saved


async def test_a_draft_goes_to_gmail_with_its_files_and_is_not_sent(
    allowed, drive, sent, gmail_drafts
):
    context, clock = _context(ASKED, on_change=clear_up)
    told = await actions.gmail_draft_email(
        context,
        ["asha@example.com"],
        "Budget",
        "Here it is.",
        "plain",
        ["drive:f-1"],
    )
    (copy_id,) = _drafted(context).payload["attachments"]
    _agreed(context, clock)

    done = await actions.save_to_gmail_drafts(context, told["action_id"])

    ((email, _),) = gmail_drafts
    assert email["To"] == "asha@example.com"
    assert [p.get_filename() for p in email.iter_parts() if p.get_filename()] == [
        "Q3 budget.pdf"
    ]
    assert sent == []
    assert "not sent" in done["status"]
    action = context.userdata.gate.get(told["action_id"])
    # It can no longer be sent from here, and says where it went.
    assert action.status == "cancelled"
    assert action.result["gmail_draft"] == (
        "https://mail.google.com/mail/?authuser=me%40example.com#drafts?compose=18c0ffee"
    )
    assert attachments.find(copy_id) is None
    with pytest.raises(ToolError, match="cancelled"):
        await actions.confirm_action(context, told["action_id"])


async def test_a_draft_is_not_put_in_gmail_before_the_user_has_heard_it(
    allowed, gmail_drafts
):
    context, _ = _context("Email Asha that I am late.")
    told = await actions.gmail_draft_email(
        context, ["asha@example.com"], "Late", "Running late."
    )

    with pytest.raises(ToolError, match="has not been told the full draft"):
        await actions.save_to_gmail_drafts(context, told["action_id"])

    assert gmail_drafts == []
    assert _drafted(context).id == told["action_id"]


async def test_only_an_email_can_be_put_in_gmail(allowed, gmail_drafts):
    context, clock = _context("Make a note of the plan.")
    monkeypatch_scopes = actions._require_write  # already allowed by the fixture
    assert monkeypatch_scopes("gdrive") is None
    told = await actions.drive_draft_document(context, "Plan", "Book the train.")
    _agreed(context, clock)

    with pytest.raises(ToolError, match="Only a drafted email"):
        await actions.save_to_gmail_drafts(context, told["action_id"])
    assert gmail_drafts == []


async def test_a_dry_run_puts_nothing_in_gmail(allowed, gmail_drafts, monkeypatch):
    monkeypatch.setenv("ORBIT_DRY_RUN", "1")
    context, clock = _context("Email Asha that I am late.")
    told = await actions.gmail_draft_email(
        context, ["asha@example.com"], "Late", "Running late."
    )
    _agreed(context, clock)

    await actions.save_to_gmail_drafts(context, told["action_id"])

    assert gmail_drafts == []
    assert context.userdata.gate.get(told["action_id"]).status == "cancelled"


# --- The files that are waiting ------------------------------------------------


async def test_every_file_the_user_added_can_be_attached_at_once(allowed):
    for name in ("one.pdf", "two.png", "three.txt"):
        attachments.keep(attachments.TRAY, name, "", PDF, "from your computer")
    context, _ = _context("Send Asha everything I added.")

    await actions.gmail_draft_email(
        context, ["asha@example.com"], "Files", "Here.", "plain", ["added:all"]
    )

    action = _drafted(context)
    assert [f["name"] for f in action.preview["attachments"]] == [
        "one.pdf",
        "two.png",
        "three.txt",
    ]
    assert len(action.payload["from_tray"]) == 3


# --- Changing a draft that carries files ---------------------------------------


def _files(context) -> list[str]:
    return [file["name"] for file in _drafted(context).preview["attachments"]]


async def test_a_draft_that_is_changed_keeps_its_files_without_being_asked_again(
    allowed, drive
):
    context, _ = _context(ASKED, on_change=clear_up)
    await actions.gmail_draft_email(
        context, ["a@example.com"], "The budget", "B", "plain", ["drive:f-1"]
    )
    _says(context, "user", "Make the subject shorter.")

    await actions.gmail_draft_email(
        context, ["a@example.com"], "Budget", "B", "plain", ["drive:f-1"]
    )

    assert _drafted(context).payload["subject"] == "Budget"
    assert _files(context) == ["Q3 budget.pdf"]


async def test_a_draft_does_not_keep_its_files_for_somebody_else(allowed, drive):
    context, _ = _context(ASKED, on_change=clear_up)
    await actions.gmail_draft_email(
        context, ["a@example.com"], "S", "B", "plain", ["drive:f-1"]
    )
    _says(context, "user", "Make the subject shorter.")

    with pytest.raises(ToolError, match="only when the user has asked"):
        await actions.gmail_draft_email(
            context, ["stranger@example.net"], "S", "B", "plain", ["drive:f-1"]
        )


async def test_a_file_put_on_a_draft_later_has_to_be_asked_for(allowed, drive):
    attachments.keep(attachments.TRAY, "photo.jpg", "", PNG, "from your computer")
    context, _ = _context(ASKED, on_change=clear_up)
    one = ["a@example.com"], "S", "B", "plain", ["drive:f-1"]
    two = ["a@example.com"], "S", "B", "plain", ["drive:f-1", "added:photo"]
    await actions.gmail_draft_email(context, *one)
    _says(context, "user", "Make it friendlier.")

    with pytest.raises(ToolError, match=r"whether they want photo\.jpg attached"):
        await actions.gmail_draft_email(context, *two)
    # The draft that was waiting is as it was.
    assert _files(context) == ["Q3 budget.pdf"]

    _says(context, "user", "And put the photo in, attached.")
    await actions.gmail_draft_email(context, *two)

    assert _files(context) == ["Q3 budget.pdf", "photo.jpg"]


async def test_a_file_the_user_handed_over_goes_when_they_say_to_send_it(allowed):
    attachments.keep(attachments.TRAY, "IMG_2041.jpeg", "", PNG, "from your computer")
    context, _ = _context("Send this to Asha.")

    await actions.gmail_draft_email(
        context, ["asha@example.com"], "Photo", "Here.", "plain", ["added:"]
    )

    assert _files(context) == ["IMG_2041.jpeg"]


async def test_a_file_fetched_from_drive_needs_more_than_being_told_to_send(
    allowed, drive
):
    context, _ = _context("Send this to Asha.")

    with pytest.raises(ToolError, match="only when the user has asked"):
        await actions.gmail_draft_email(
            context, ["asha@example.com"], "S", "B", "plain", ["drive:f-1"]
        )


# --- Asking the user for a file from their computer ----------------------------


async def test_asked_for_a_file_the_user_is_shown_where_to_pick_it(shown):
    context, _ = _context("Email Asha the signed contract from my desktop.")

    told = await attaching.ask_for_file(context, "  the signed\ncontract ")

    assert shown == [
        {"id": "upload", "kind": "upload", "asking": "the signed contract"}
    ]
    assert "draft nothing yet" in told["next"]


async def test_a_file_already_handed_over_is_not_asked_for_again(shown):
    attachments.keep(attachments.TRAY, "site-photo.png", "", PNG, "from your computer")
    attachments.keep(attachments.TRAY, "Trip plan.md", "", PDF, "written by Orbit")
    context, _ = _context("I just dropped a photo of the site. Email it to Asha.")

    told = await attaching.ask_for_file(context, "a photo of the site")

    # The one the user handed over, not the one Orbit wrote, and no window.
    assert told["handed_over_already"] == [
        {
            "name": "site-photo.png",
            "size": "18 bytes",
            "attach_as": "added:site-photo.png",
        }
    ]
    assert shown == []
    assert context.userdata.asked_for_file is None

    # Asked again, it is not that one that is meant: now the user is asked.
    again = await attaching.ask_for_file(context, "the signed contract")

    assert "handed_over_already" not in again
    assert shown == [
        {"id": "upload", "kind": "upload", "asking": "the signed contract"}
    ]


async def _asked(context, what: str = "the signed contract") -> None:
    """The user is asked for a file: whatever was waiting has been told of first."""
    while "handed_over_already" in await attaching.ask_for_file(context, what):
        pass


async def test_a_file_asked_for_is_not_stood_in_for_by_one_that_was_waiting(
    allowed, shown
):
    attachments.keep(attachments.TRAY, "old-scan.pdf", "", PDF, "from your computer")
    context, _ = _context("Email Asha the signed contract from my desktop.")
    await _asked(context)
    draft = ["asha@example.com"], "Contract", "Attached.", "plain"

    # Drafted in the same breath as asking: the user has not picked anything yet.
    for reference in ("added:latest", "added:all", "added:old-scan"):
        with pytest.raises(ToolError, match="has not picked the file"):
            await actions.gmail_draft_email(context, *draft, [reference])
    assert context.userdata.gate.awaiting() is None

    # They pick it, and the app says so for them.
    attachments.keep(attachments.TRAY, "contract.pdf", "", PDF, "from your computer")
    _says(context, "user", "Attach the file I just added: contract.pdf.")
    await actions.gmail_draft_email(context, *draft, ["added:contract.pdf"])

    assert _files(context) == ["contract.pdf"]
    # The asking is over: a later draft is not held up by it.
    assert context.userdata.asked_for_file is None


async def test_a_file_never_handed_over_is_not_waited_for_for_ever(allowed, shown):
    attachments.keep(attachments.TRAY, "old-scan.pdf", "", PDF, "from your computer")
    context, clock = _context("Email Asha the signed contract from my desktop.")
    await _asked(context)
    clock.now += attaching.ASKED_FOR_SECONDS + 1
    _says(context, "user", "Never mind that. Send Asha the old scan I added.")

    await actions.gmail_draft_email(
        context, ["asha@example.com"], "Scan", "Here.", "plain", ["added:old-scan"]
    )

    assert _files(context) == ["old-scan.pdf"]


async def test_being_asked_for_a_file_does_not_hold_up_one_from_drive(
    allowed, drive, shown
):
    context, _ = _context(ASKED)
    await _asked(context)

    await actions.gmail_draft_email(
        context, ["asha@example.com"], "Budget", "Here.", "plain", ["drive:f-1"]
    )

    assert _files(context) == ["Q3 budget.pdf"]
