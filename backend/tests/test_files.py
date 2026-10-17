import base64

import pytest
from fastapi import HTTPException
from livekit.agents.llm import ToolError

import files
from agent_tools import google
from api import files as files_api
from api import mail as mail_api
from integrations.google import api, gdrive, gmail
from integrations.google.api import GoogleApiError, NotConnectedError, TooLargeError

DOC = "application/vnd.google-apps.document"
WORD = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PREVIEW = "https://lh3.googleusercontent.com/drive-storage/abc=s220"
PNG = b"\x89PNG\r\n\x1a\n rest of a picture"


@pytest.mark.parametrize(
    ("name", "mime_type", "view"),
    [
        ("logo.png", "image/png", "image"),
        ("scan.JPG", "application/octet-stream", "image"),
        ("drawing.svg", "image/svg+xml", "image"),
        ("contract.pdf", "application/pdf", "pdf"),
        ("contract.pdf", "", "pdf"),
        ("notes.txt", "text/plain; charset=utf-8", "text"),
        ("data.json", "application/json", "text"),
        ("page.html", "text/html", "html"),
        ("page.htm", "application/octet-stream", "html"),
        ("deploy.sh", "application/octet-stream", "text"),
        ("README.md", "text/markdown", "markdown"),
        ("README.md", "text/plain", "markdown"),
        ("README.md", "application/octet-stream", "markdown"),
        ("photo.heic", "image/heic", "none"),
        ("report.docx", WORD, "none"),
        ("backup.zip", "application/zip", "none"),
        # A name is only believed when the type says nothing.
        ("notes.md", "application/zip", "none"),
        ("picture.png", "application/pdf", "pdf"),
    ],
)
def test_a_file_is_shown_by_what_its_type_and_name_say(name, mime_type, view):
    assert files.view_of(name, mime_type) == view


def test_text_never_goes_to_the_browser_as_something_it_would_run():
    assert files.served_as("page.html", "text/html") == files.PLAIN_TEXT
    assert files.served_as("app.js", "application/javascript") == files.PLAIN_TEXT
    assert files.served_as("README.md", "text/markdown") == files.PLAIN_TEXT
    assert files.served_as("scan.JPG", "application/octet-stream") == "image/jpeg"
    assert files.served_as("backup.zip", "application/zip") is None


def test_text_is_read_whatever_way_it_was_saved():
    assert files.decode("naïve".encode()) == "naïve"
    assert files.decode(b"\xef\xbb\xbfwith a mark") == "with a mark"
    assert files.decode("naïve".encode("utf-16")) == "naïve"
    # Bytes that are not text are shown as such, not dropped.
    assert files.decode(b"a\xffb") == "a�b"
    assert files.as_text(b"abcdef", 4) == ("abcd", True)
    assert files.as_text(b"abcd", 4) == ("abcd", False)


def _item(name: str, mime_type: str, **more) -> dict:
    return {
        "id": "f-1",
        "name": name,
        "mimeType": mime_type,
        "modifiedTime": "2026-10-01T09:30:00Z",
        "owners": [{"displayName": "Asha Verma"}],
        "webViewLink": "https://drive.google.com/file/d/f-1/view",
        **more,
    }


@pytest.fixture
def drive(monkeypatch):
    """A stand-in for Drive holding one file. Records what was fetched from it."""
    held: dict = {"item": _item("logo.png", "image/png"), "bytes": PNG, "text": ""}
    fetched: list[dict] = []

    async def get_json(session, url, params=None, **_):
        return held["item"]

    async def get_bytes(session, url, params=None, *, limit):
        fetched.append({"url": url, **(params or {})})
        if len(held["bytes"]) > limit:
            raise TooLargeError
        return held["bytes"]

    async def get_text(session, url, params=None):
        fetched.append({"url": url, **(params or {})})
        return held["text"]

    monkeypatch.setattr(api, "get_json", get_json)
    monkeypatch.setattr(api, "get_bytes", get_bytes)
    monkeypatch.setattr(api, "get_text", get_text)
    return held, fetched


async def test_a_picture_is_shown_as_it_is(drive):
    _, fetched = drive

    name, media_type, data = await gdrive.shown(None, "f-1")

    assert (name, media_type, data) == ("logo.png", "image/png", PNG)
    assert fetched == [{"url": f"{gdrive._API}/files/f-1", "alt": "media"}]


async def test_a_google_doc_is_shown_as_a_pdf_of_itself(drive):
    held, fetched = drive
    held["item"] = _item("Trip notes", DOC)
    held["bytes"] = b"%PDF-1.7 the document"

    look = await gdrive.look(None, "f-1")
    _, media_type, data = await gdrive.shown(None, "f-1")

    assert (look["view"], look["preview"], look["kind"]) == ("pdf", False, "Google Doc")
    assert (media_type, data) == ("application/pdf", b"%PDF-1.7 the document")
    assert fetched == [
        {"url": f"{gdrive._API}/files/f-1/export", "mimeType": "application/pdf"}
    ]


async def test_a_document_too_long_for_a_pdf_is_shown_as_its_text(drive, monkeypatch):
    held, _ = drive
    held["item"] = _item("The long report", DOC)
    asked: list[str] = []

    async def get_bytes(session, url, params=None, *, limit):
        asked.append(params["mimeType"])
        if params["mimeType"] == "application/pdf":
            raise GoogleApiError(403, "This file is too large to be exported.")
        return b"Chapter one."

    monkeypatch.setattr(api, "get_bytes", get_bytes)

    _, media_type, data = await gdrive.shown(None, "f-1")

    assert (media_type, data) == ("text/plain; charset=utf-8", b"Chapter one.")
    assert asked == ["application/pdf", "text/plain"]


async def test_any_other_refusal_to_make_a_pdf_is_passed_on(drive, monkeypatch):
    held, _ = drive
    held["item"] = _item("Private notes", DOC)

    async def get_bytes(session, url, params=None, *, limit):
        raise GoogleApiError(403, "The user does not have sufficient permissions.")

    monkeypatch.setattr(api, "get_bytes", get_bytes)

    with pytest.raises(GoogleApiError):
        await gdrive.shown(None, "f-1")


async def test_a_file_a_browser_cannot_draw_is_shown_by_drives_picture_of_it(drive):
    held, fetched = drive
    held["item"] = _item("Report.docx", WORD, thumbnailLink=PREVIEW)

    look = await gdrive.look(None, "f-1")
    _, media_type, _ = await gdrive.shown(None, "f-1")

    assert (look["view"], look["preview"]) == ("image", True)
    assert look["kind"] == "Word document"
    assert media_type == "image/png"
    # Asked for at a size worth looking at, not the thumbnail Drive offers.
    assert fetched == [{"url": PREVIEW.replace("=s220", "=s1600")}]


async def test_a_preview_is_only_ever_fetched_from_google(drive):
    held, fetched = drive
    held["item"] = _item(
        "Report.docx", WORD, thumbnailLink="https://example.com/steal=s220"
    )

    look = await gdrive.look(None, "f-1")

    assert look["view"] == "none"
    with pytest.raises(gdrive.UnreadableFileError):
        await gdrive.shown(None, "f-1")
    assert fetched == []


async def test_a_markdown_file_comes_to_the_app_drawn_and_whole(drive):
    held, _ = drive
    held["item"] = _item("README.md", "text/plain")
    held["bytes"] = ("# Plan\n\n" + "A long line.\n" * 2000 + "\nThe end.").encode()

    look = await gdrive.look(None, "f-1")

    assert look["view"] == "markdown"
    assert look["kind"] == "Markdown file"
    assert look["html"].startswith("<h1>Plan</h1>")
    assert look["html"].endswith("<p>The end.</p>")
    # Lines run on into a paragraph, as a Markdown file is read.
    assert "<br>" not in look["html"]
    assert look["html"].count("A long line.") == 2000
    assert look["text"] is None
    assert look["text_truncated"] is False


async def test_text_too_long_to_show_is_cut_and_said_to_be(drive, monkeypatch):
    held, _ = drive
    held["item"] = _item("log.txt", "text/plain")
    held["bytes"] = b"x" * 500
    monkeypatch.setattr(gdrive, "MAX_SHOWN_CHARS", 100)

    look = await gdrive.look(None, "f-1")

    assert len(look["text"]) == 100
    assert look["text_truncated"] is True


async def test_a_file_too_large_to_show_is_refused_not_cut(drive, monkeypatch):
    held, _ = drive
    held["item"] = _item("scan.pdf", "application/pdf")
    held["bytes"] = b"%PDF" + b"x" * 500
    monkeypatch.setattr(gdrive, "MAX_SHOWN_BYTES", 100)

    with pytest.raises(TooLargeError):
        await gdrive.shown(None, "f-1")


async def test_the_agent_is_given_a_documents_text(drive):
    held, _ = drive
    held["item"] = _item("Trip notes", DOC)
    held["text"] = "Book the train."

    file = await gdrive.read(None, "f-1")

    assert file["content"] == "Book the train."
    assert file["kind"] == "Google Doc"


async def test_the_agent_is_given_the_words_of_an_html_file_not_its_tags(drive):
    held, _ = drive
    held["item"] = _item("page.html", "text/html")
    held["bytes"] = (
        b"<html><head><style>p{color:red}</style></head><body><h1>Plan</h1>"
        b"<ul><li>Book the train<li>Pack</ul><script>x()</script></body></html>"
    )

    file = await gdrive.read(None, "f-1")

    assert file["content"] == "Plan\n\n- Book the train\n- Pack"
    assert file["kind"] == "HTML file"


async def test_the_agent_is_given_no_text_for_a_pdf_or_a_picture(drive):
    held, fetched = drive
    for name, mime_type, kind in [
        ("scan.pdf", "application/pdf", "PDF"),
        ("logo.png", "image/png", "picture"),
    ]:
        held["item"] = _item(name, mime_type)

        file = await gdrive.read(None, "f-1")

        assert file["content"] is None
        assert file["content_truncated"] is False
        assert file["kind"] == kind
    # Nothing was downloaded to find that out.
    assert fetched == []


async def test_a_file_that_can_be_neither_read_nor_shown_is_refused(drive):
    held, _ = drive
    held["item"] = _item("backup.zip", "application/zip")

    with pytest.raises(gdrive.UnreadableFileError, match="application/zip"):
        await gdrive.read(None, "f-1")


# --- The routes the app fetches a file from ---------------------------------


async def test_a_picture_reaches_the_browser_only_to_be_drawn(drive):
    held, _ = drive
    held["item"] = _item("über logo.svg", "image/svg+xml")
    held["bytes"] = b"<svg><script>alert(1)</script></svg>"

    response = await files_api.drive_file_content("f-1")

    assert response.media_type == "image/svg+xml"
    assert response.body == held["bytes"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
    # Opened in a tab of its own, it may run nothing.
    assert "sandbox" in response.headers["content-security-policy"]
    assert response.headers["content-disposition"] == (
        "inline; filename*=UTF-8''%C3%BCber%20logo.svg"
    )


async def test_a_pdf_is_left_to_the_browsers_own_viewer(drive):
    held, _ = drive
    held["item"] = _item("scan.pdf", "application/pdf")
    held["bytes"] = b"%PDF-1.7"

    response = await files_api.drive_file_content("f-1")

    assert response.media_type == "application/pdf"
    # A sandbox would switch the viewer off.
    assert "content-security-policy" not in response.headers
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_a_web_page_kept_in_drive_is_drawn_without_what_it_would_run(drive):
    held, _ = drive
    held["item"] = _item("page.html", "text/html")
    held["bytes"] = (
        b"<html><head><title>T</title><style>p{color:red}</style></head><body>"
        b'<h1 onclick="steal()">Plan</h1><script>alert(1)</script>'
        b'<p style="display:none">Read me</p><img src="https://x.example/t.gif">'
        b"</body></html>"
    )

    info = await files_api.drive_file("f-1")
    response = await files_api.drive_file_content("f-1")

    assert (info.view, info.kind, info.text) == ("html", "HTML file", None)
    assert info.html == "<h1>Plan</h1><p>Read me</p>"
    # Opened in a tab of its own, it is its source and nothing a browser runs.
    assert response.media_type == "text/plain; charset=utf-8"


async def test_the_app_is_told_what_a_file_is_and_where_it_opens(drive):
    held, _ = drive
    held["item"] = _item("Report.docx", WORD, thumbnailLink=PREVIEW)

    info = await files_api.drive_file("f-1")

    assert info.model_dump() == {
        "name": "Report.docx",
        "kind": "Word document",
        "view": "image",
        "preview": True,
        "text": None,
        "html": None,
        "truncated": False,
        "link": "https://drive.google.com/file/d/f-1/view",
    }


@pytest.mark.parametrize(
    ("failure", "status"),
    [
        (NotConnectedError(), 503),
        (GoogleApiError(404, "File not found"), 404),
        (TooLargeError(), 413),
        (gdrive.UnreadableFileError("application/zip"), 415),
        (gmail.NoSuchAttachmentError(), 404),
        (TimeoutError(), 502),
    ],
)
async def test_what_goes_wrong_reaches_the_app_with_its_reason(
    failure, status, monkeypatch
):
    async def fail(session, file_id):
        raise failure

    monkeypatch.setattr(gdrive, "shown", fail)

    with pytest.raises(HTTPException) as refused:
        await files_api.drive_file_content("f-1")

    assert refused.value.status_code == status


# --- Files that came with an email ------------------------------------------


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


MESSAGE = {
    "id": "m-1",
    "internalDate": "1790000000000",
    "payload": {
        "partId": "",
        "mimeType": "multipart/mixed",
        "filename": "",
        "headers": [{"name": "From", "value": "Asha Verma <asha@example.com>"}],
        "parts": [
            {
                "partId": "0",
                "mimeType": "multipart/alternative",
                "filename": "",
                "parts": [
                    {
                        "partId": "0.0",
                        "mimeType": "text/plain",
                        "filename": "",
                        "body": {"data": _b64(b"Invoice attached.")},
                    }
                ],
            },
            {
                "partId": "1",
                "mimeType": "application/pdf",
                "filename": "Invoice 2291.pdf",
                "body": {"attachmentId": "changes-every-time", "size": 2048},
            },
            {
                "partId": "2",
                "mimeType": "application/octet-stream",
                "filename": "notes.md",
                "body": {"data": _b64(b"# Notes\n\nPay by Friday."), "size": 23},
            },
            {
                "partId": "3",
                "mimeType": "application/zip",
                "filename": "source.zip",
                "body": {"attachmentId": "zip", "size": 9000},
            },
        ],
    },
}


@pytest.fixture
def inbox(monkeypatch):
    """A stand-in for Gmail holding MESSAGE. Records which attachments were fetched."""
    fetched: list[str] = []

    async def get_json(session, url, params=None, **_):
        if "/attachments/" in url:
            fetched.append(url.rsplit("/", 1)[1])
            return {"data": _b64(b"%PDF-1.7 the invoice")}
        return MESSAGE

    monkeypatch.setattr(api, "get_json", get_json)
    return fetched


def test_an_emails_files_are_found_wherever_they_are_nested():
    assert gmail.attachments(MESSAGE) == [
        {"part": "1", "name": "Invoice 2291.pdf", "mime_type": "application/pdf"},
        {"part": "2", "name": "notes.md", "mime_type": "application/octet-stream"},
        {"part": "3", "name": "source.zip", "mime_type": "application/zip"},
    ]
    # The agent still gets their names.
    assert gmail.full(MESSAGE)["attachments"] == [
        "Invoice 2291.pdf",
        "notes.md",
        "source.zip",
    ]


async def test_an_attachment_is_fetched_by_the_part_it_is_in(inbox):
    response = await files_api.attachment_content("m-1", "1")

    assert response.media_type == "application/pdf"
    assert response.body == b"%PDF-1.7 the invoice"
    assert inbox == ["changes-every-time"]


async def test_a_small_attachment_that_came_with_the_email_is_not_fetched_again(inbox):
    info = await files_api.attachment("m-1", "2")
    response = await files_api.attachment_content("m-1", "2")

    assert (info.view, info.kind) == ("markdown", "Markdown file")
    assert info.html == "<h1>Notes</h1>\n<p>Pay by Friday.</p>"
    assert response.media_type == "text/plain; charset=utf-8"
    assert inbox == []


async def test_saying_what_a_pdf_attachment_is_does_not_download_it(inbox):
    info = await files_api.attachment("m-1", "1")

    assert (info.name, info.view, info.text) == ("Invoice 2291.pdf", "pdf", None)
    assert inbox == []


async def test_an_attachment_the_app_cannot_show_or_find_is_refused(inbox):
    with pytest.raises(HTTPException) as unshowable:
        await files_api.attachment_content("m-1", "3")
    with pytest.raises(HTTPException) as missing:
        await files_api.attachment_content("m-1", "7")

    assert unshowable.value.status_code == 415
    assert missing.value.status_code == 404
    assert inbox == []


async def test_an_email_lists_its_attachments_as_files_the_app_can_open(
    inbox, tmp_path, monkeypatch
):
    monkeypatch.setenv("ORBIT_DATA_DIR", str(tmp_path))

    email = await mail_api.message("m-1")

    assert [a.model_dump() for a in email.attachments] == [
        {"name": "Invoice 2291.pdf", "part": "1", "view": "pdf"},
        {"name": "notes.md", "part": "2", "view": "markdown"},
        {"name": "source.zip", "part": "3", "view": "none"},
    ]


# --- What the agent's tools do with a file -----------------------------------


@pytest.fixture
def shown(monkeypatch) -> list[dict]:
    """The cards the tools asked the app to put on screen."""
    cards: list[dict] = []

    async def show(card, *, push=None):
        cards.append(card)

    monkeypatch.setattr(google.stage, "show", show)
    monkeypatch.setattr(google.utils.http_context, "http_session", lambda: None)
    return cards


async def test_a_document_that_is_read_is_put_on_screen_itself(shown, drive):
    held, _ = drive
    held["item"] = _item("Trip notes", DOC)
    held["text"] = "Book the train."

    file = await google.drive_read(None, "f-1")

    assert shown == [
        {
            "id": "file:f-1",
            "kind": "file",
            "from": "drive",
            "file_id": "f-1",
            "title": "Trip notes",
            "tag": "Google Doc",
        }
    ]
    assert file["content"] == "Book the train."
    assert file["on_screen"] is True
    assert file["link"] == "https://drive.google.com/file/d/f-1/view"
    assert "note" not in file


@pytest.mark.parametrize(
    ("name", "mime_type", "kind"),
    [("logo.png", "image/png", "picture"), ("scan.pdf", "application/pdf", "PDF")],
)
async def test_a_file_orbit_cannot_read_is_shown_and_the_model_told_so(
    shown, drive, name, mime_type, kind
):
    held, _ = drive
    held["item"] = _item(name, mime_type)

    file = await google.drive_read(None, "f-1")

    (card,) = shown
    assert (card["kind"], card["file_id"], card["tag"]) == ("file", "f-1", kind)
    assert file["content"] is None
    assert file["on_screen"] is True
    assert "cannot see what is in it" in file["note"]


async def test_a_file_that_cannot_be_shown_puts_nothing_on_screen(shown, drive):
    held, _ = drive
    held["item"] = _item("backup.zip", "application/zip")

    with pytest.raises(ToolError, match="neither read nor show"):
        await google.drive_read(None, "f-1")
    assert shown == []


async def test_files_found_can_each_be_shown_from_the_card_but_folders_cannot(
    shown, monkeypatch
):
    async def search(session, text, limit):
        return [
            {**_FOUND, "id": "p-1", "name": "March.pdf", "kind": "PDF"},
            {**_FOUND, "id": "d-1", "name": "Invoices", "kind": "folder"},
        ]

    monkeypatch.setattr(gdrive, "search", search)

    await google.drive_search(None, "march")

    (card,) = shown
    assert [row.get("file_id") for row in card["rows"]] == ["p-1", None]


_FOUND = {"modified": "Thu 01 Oct 2026 15:00", "owner": "Asha Verma", "link": None}


async def test_an_attachment_is_opened_by_its_name(shown, inbox):
    opened = await google.gmail_open_attachment(None, "m-1", "invoice 2291.PDF")

    assert shown == [
        {
            "id": "file:m-1:1",
            "kind": "file",
            "from": "mail",
            "message_id": "m-1",
            "part": "1",
            "title": "Invoice 2291.pdf",
            "tag": "PDF",
        }
    ]
    assert opened["name"] == "Invoice 2291.pdf"
    assert opened["content"] is None
    assert "cannot see what is in it" in opened["note"]
    # The model is only told it is there. The app fetches it when it shows it.
    assert inbox == []


async def test_a_text_attachment_is_shown_and_can_be_read_out(shown, inbox):
    opened = await google.gmail_open_attachment(None, "m-1", "notes")

    (card,) = shown
    assert card["part"] == "2"
    assert opened["content"] == "# Notes\n\nPay by Friday."
    assert opened["content_truncated"] is False
    assert "note" not in opened


async def test_a_name_that_fits_no_one_attachment_is_asked_about(shown, inbox):
    with pytest.raises(ToolError) as unsure:
        await google.gmail_open_attachment(None, "m-1", "")

    assert "Invoice 2291.pdf, notes.md, source.zip" in str(unsure.value)
    assert shown == []


async def test_an_attachment_orbit_cannot_show_is_said_to_be_in_gmail(shown, inbox):
    with pytest.raises(ToolError, match="cannot show"):
        await google.gmail_open_attachment(None, "m-1", "source.zip")
    assert shown == []


def test_the_only_attachment_is_the_one_meant_whatever_it_is_called():
    only = [{"part": "1", "name": "IMG_2041.jpeg", "mime_type": "image/jpeg"}]
    two = [*only, {"part": "2", "name": "IMG_2042.jpeg", "mime_type": "image/jpeg"}]

    assert google._attachment_meant(only, "the photo") == only[0]
    assert google._attachment_meant(two, "the photo") is None
    assert google._attachment_meant(two, "2042") == two[1]
    assert google._attachment_meant(two, "IMG_2041.JPEG") == two[0]
