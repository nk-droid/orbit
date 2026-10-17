from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from livekit.agents.llm import ToolError

from actions.effects import perform
from agent_tools import actions, google
from agent_tools.actions import _why_not, whole_links
from integrations.google import api, gdrive
from integrations.google.api import GoogleApiError


@pytest.fixture(autouse=True)
def signed_in(monkeypatch):
    """No real sign-in: requests go out without a Google token."""

    async def no_token() -> dict[str, str]:
        return {}

    monkeypatch.setattr(api, "_headers", no_token)


async def test_a_document_is_created_in_one_upload_with_its_text(monkeypatch):
    received: dict = {}

    async def upload(request: web.Request) -> web.Response:
        received["query"] = dict(request.query)
        parts = await request.multipart()
        described = await parts.next()
        received["description"] = await described.json()
        text = await parts.next()
        received["text_type"] = text.headers["Content-Type"]
        received["text"] = await text.text()
        received["extra_part"] = await parts.next()
        return web.json_response(
            {"id": "doc-1", "name": "Trip notes", "webViewLink": "https://docs/doc-1"}
        )

    app = web.Application()
    app.router.add_post("/upload", upload)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        monkeypatch.setattr(gdrive, "_UPLOAD", str(server.make_url("/upload")))

        created = await gdrive.create_document(
            session, "Trip notes", "Book the train.\nPack light."
        )

    assert created == {
        "id": "doc-1",
        "name": "Trip notes",
        "link": "https://docs/doc-1",
    }
    assert received["query"]["uploadType"] == "multipart"
    assert received["description"] == {
        "name": "Trip notes",
        "mimeType": "application/vnd.google-apps.document",
    }
    assert received["text_type"].startswith("text/plain")
    assert received["text"] == "Book the train.\nPack light."
    assert received["extra_part"] is None


async def test_text_is_added_at_the_end_of_a_document_on_a_new_line(monkeypatch):
    sent: list[tuple] = []

    async def send_json(session, method, url, body, params=None):
        sent.append((method, url, body))
        return {}

    monkeypatch.setattr(api, "send_json", send_json)

    await gdrive.append_text(None, "doc-1", "Buy milk.")

    ((method, url, body),) = sent
    assert method == "POST"
    assert url.endswith("/documents/doc-1:batchUpdate")
    assert body == {
        "requests": [
            {"insertText": {"endOfSegmentLocation": {}, "text": "\nBuy milk."}}
        ]
    }


async def test_an_approved_document_reaches_drive_as_drafted(monkeypatch):
    created: list[tuple] = []

    async def create_document(session, title, text, *, html=None):
        created.append((title, text, html))
        return {"id": "doc-1", "name": title, "link": None}

    monkeypatch.setattr(gdrive, "create_document", create_document)
    payload = {"title": "Packing list", "content": "Passport"}

    await perform(None, "drive.create_document", payload)

    assert created == [("Packing list", "Passport", None)]


def test_a_disabled_api_is_reported_as_something_the_user_can_fix():
    error = GoogleApiError(
        403,
        "Google Docs API has not been used in project 123 before or it is disabled.",
    )

    reason = _why_not("drive.add_to_document", error)

    assert "not enabled" in reason
    assert "Google Docs API" in reason


def test_a_document_orbit_did_not_create_is_explained():
    reason = _why_not("drive.add_to_document", GoogleApiError(404, "Not found"))

    assert "created itself" in reason


def _item(file_id: str, name: str, mime: str = "application/pdf") -> dict:
    return {
        "id": file_id,
        "name": name,
        "mimeType": mime,
        "modifiedTime": "2026-10-01T09:30:00Z",
        "owners": [{"displayName": "Asha Verma"}],
        "webViewLink": f"https://drive.google.com/file/d/{file_id}/view",
    }


def _found(file_id: str, name: str, kind: str = "folder") -> dict:
    return {
        "id": file_id,
        "name": name,
        "kind": kind,
        "modified": "Thu 01 Oct 2026 15:00",
        "owner": "Asha Verma",
        "link": f"https://drive.google.com/open?id={file_id}",
    }


FOLDER = "application/vnd.google-apps.folder"


@pytest.fixture
def drive(monkeypatch):
    """A stand-in for Drive's file listing that records what it was asked."""
    asked: list[dict] = []
    answer: dict = {"files": []}

    async def get_json(session, url, params=None):
        asked.append({"url": url, **params})
        return answer

    monkeypatch.setattr(api, "get_json", get_json)
    return asked, answer


async def test_a_folder_is_listed_by_asking_for_what_is_kept_in_it(drive):
    asked, answer = drive
    answer["files"] = [_item("f-2", "2024", FOLDER), _item("p-1", "March.pdf")]
    answer["nextPageToken"] = "next"

    listing = await gdrive.children(None, "f-1", limit=2)

    assert asked[0]["q"] == "'f-1' in parents and trashed = false"
    assert asked[0]["orderBy"] == "folder,name"
    assert asked[0]["pageSize"] == 2
    assert [(i["name"], i["kind"]) for i in listing["items"]] == [
        ("2024", "folder"),
        ("March.pdf", "PDF"),
    ]
    assert listing["more"] is True
    # The address that opens each one is asked for, and passed on.
    assert "webViewLink" in asked[0]["fields"]
    assert [i["link"] for i in listing["items"]] == [
        "https://drive.google.com/file/d/f-2/view",
        "https://drive.google.com/file/d/p-1/view",
    ]


async def test_a_file_found_by_its_words_comes_with_its_link(drive):
    asked, answer = drive
    answer["files"] = [
        _item("d-1", "Budget", "application/vnd.google-apps.spreadsheet")
    ]

    (file,) = await gdrive.search(None, "budget")

    assert "webViewLink" in asked[0]["fields"]
    assert file["link"] == "https://drive.google.com/file/d/d-1/view"


async def test_a_file_drive_gives_no_link_for_has_none(drive):
    _, answer = drive
    answer["files"] = [{**_item("d-1", "Budget"), "webViewLink": None}]

    (file,) = await gdrive.search(None, "budget")

    assert file["link"] is None


async def test_with_no_folder_named_the_top_of_my_drive_is_listed(drive):
    asked, _ = drive

    listing = await gdrive.children(None)

    assert asked[0]["q"] == "'root' in parents and trashed = false"
    assert listing == {"items": [], "more": False}


async def test_a_folder_is_looked_up_by_name_among_folders_only(drive):
    asked, answer = drive
    answer["files"] = [_item("f-1", "Asha's invoices", FOLDER)]

    folders = await gdrive.folders_named(None, "Asha's invoices")

    # The apostrophe is escaped, so the name cannot end the quoted string early.
    assert asked[0]["q"] == (
        "name contains 'Asha\\'s invoices' "
        "and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    )
    assert [f["id"] for f in folders] == ["f-1"]


@pytest.mark.parametrize(
    ("names", "said", "meant"),
    [
        (["Invoices"], "invoices", "Invoices"),
        (["Invoices 2024"], "invoices", "Invoices 2024"),
        (["Invoices", "Invoices 2024"], "Invoices", "Invoices"),
        (["Invoices 2023", "Invoices 2024"], "invoices", None),
        (["Invoices", "Invoices"], "invoices", None),
        ([], "invoices", None),
    ],
)
def test_a_name_picks_a_folder_only_when_it_points_to_one(names, said, meant):
    folders = [_found(f"f-{n}", name) for n, name in enumerate(names)]

    picked = google._meant(folders, said)

    assert (picked and picked["name"]) == meant


@pytest.fixture
def shown(monkeypatch) -> list[dict]:
    """The cards the folder tool asked the app to put on screen."""
    cards: list[dict] = []

    async def show(card, *, push=None):
        cards.append(card)

    monkeypatch.setattr(google.stage, "show", show)
    monkeypatch.setattr(google.utils.http_context, "http_session", lambda: None)
    return cards


async def test_a_folder_named_by_the_user_is_found_and_its_contents_listed(
    shown, monkeypatch
):
    listed: list[tuple] = []

    async def folders_named(session, name):
        return [_found("f-1", "Invoices")]

    async def children(session, folder_id, limit):
        listed.append((folder_id, limit))
        return {"items": [_found("p-1", "March.pdf", "PDF")], "more": True}

    monkeypatch.setattr(gdrive, "folders_named", folders_named)
    monkeypatch.setattr(gdrive, "children", children)

    result = await google.drive_list_folder(None, folder=" invoices ")

    assert listed == [("f-1", 20)]
    assert result == {
        "folder": {
            "id": "f-1",
            "name": "Invoices",
            "link": "https://drive.google.com/open?id=f-1",
        },
        "items": [_found("p-1", "March.pdf", "PDF")],
        "count": 1,
        "more_not_listed": True,
    }
    (card,) = shown
    assert (card["id"], card["title"], card["tag"]) == (
        "found:drive",
        "Invoices",
        "1+ inside",
    )
    assert card["rows"][0]["title"] == "March.pdf"
    # The card opens the folder, and each entry opens its own file.
    assert card["link"] == "https://drive.google.com/open?id=f-1"
    assert card["link_label"] == "Open folder in Drive"
    assert card["rows"][0]["link"] == "https://drive.google.com/open?id=p-1"


async def test_every_item_of_a_folder_is_on_the_card_with_its_link(shown, monkeypatch):
    items = [_found(f"p-{n}", f"Page {n}.pdf", "PDF") for n in range(20)]

    async def describe(session, file_id):
        return _found(file_id, "Scans")

    async def children(session, folder_id, limit):
        return {"items": items, "more": False}

    monkeypatch.setattr(gdrive, "describe", describe)
    monkeypatch.setattr(gdrive, "children", children)

    await google.drive_list_folder(None, folder_id="f-9")

    (card,) = shown
    assert len(card["rows"]) == 20
    assert card["more"] == 0
    assert [row["link"] for row in card["rows"]] == [item["link"] for item in items]


async def test_files_found_are_all_on_the_card_with_their_links(shown, monkeypatch):
    files = [_found(f"d-{n}", f"Notes {n}", "Google Doc") for n in range(10)]

    async def search(session, text, limit):
        return files

    monkeypatch.setattr(gdrive, "search", search)

    result = await google.drive_search(None, text="notes", max_results=10)

    (card,) = shown
    assert [row["link"] for row in card["rows"]] == [file["link"] for file in files]
    assert result["files"][0]["link"] == "https://drive.google.com/open?id=d-0"


async def test_a_file_that_is_read_is_shown_and_comes_with_its_link(shown, monkeypatch):
    async def read(session, file_id, *, sheets_as_text=True):
        return {
            **_found(file_id, "Trip notes", "Google Doc"),
            "content": "Book the train.",
            "content_truncated": False,
        }

    monkeypatch.setattr(gdrive, "read", read)

    file = await google.drive_read(None, "d-1")

    # The card names the file. The app fetches it, and its link, itself.
    (card,) = shown
    assert (card["kind"], card["from"], card["file_id"]) == ("file", "drive", "d-1")
    assert file["link"] == "https://drive.google.com/open?id=d-1"


async def test_several_folders_of_that_name_are_offered_and_none_is_opened(
    shown, monkeypatch
):
    same_name = [_found("f-1", "Invoices"), _found("f-2", "Invoices")]

    async def folders_named(session, name):
        return same_name

    async def children(session, folder_id, limit):
        raise AssertionError("no folder was chosen, so none may be listed")

    monkeypatch.setattr(gdrive, "folders_named", folders_named)
    monkeypatch.setattr(gdrive, "children", children)

    result = await google.drive_list_folder(None, folder="Invoices")

    assert result == {"folder": None, "folders_matching": same_name}
    (card,) = shown
    assert (card["title"], card["tag"]) == ("Drive", "2 folders")


async def test_a_name_no_folder_has_lists_nothing_and_shows_nothing(shown, monkeypatch):
    async def folders_named(session, name):
        return []

    monkeypatch.setattr(gdrive, "folders_named", folders_named)

    result = await google.drive_list_folder(None, folder="Invoices")

    assert result == {"folder": None, "folders_matching": []}
    assert shown == []


async def test_a_folder_reached_by_id_is_listed_under_its_own_name(shown, monkeypatch):
    async def describe(session, file_id):
        return _found(file_id, "2024")

    async def children(session, folder_id, limit):
        return {"items": [], "more": False}

    monkeypatch.setattr(gdrive, "describe", describe)
    monkeypatch.setattr(gdrive, "children", children)

    result = await google.drive_list_folder(None, folder_id="f-2", max_results=500)

    assert result["folder"] == {
        "id": "f-2",
        "name": "2024",
        "link": "https://drive.google.com/open?id=f-2",
    }
    assert (result["count"], result["more_not_listed"]) == (0, False)
    assert (shown[0]["title"], shown[0]["tag"]) == ("2024", "0 inside")


async def test_with_no_folder_given_the_top_of_my_drive_is_listed(shown, monkeypatch):
    listed: list[tuple] = []

    async def children(session, folder_id, limit):
        listed.append((folder_id, limit))
        return {"items": [], "more": False}

    monkeypatch.setattr(gdrive, "children", children)

    result = await google.drive_list_folder(None, max_results=500)

    # The most that is ever asked for, however many were requested.
    assert listed == [("root", google.MAX_FOLDER_ITEMS)]
    assert result["folder"] == {
        "id": "root",
        "name": "My Drive",
        "link": "https://drive.google.com/drive/my-drive",
    }


async def test_a_file_given_as_a_folder_is_refused_not_shown_as_empty(
    shown, monkeypatch
):
    async def describe(session, file_id):
        return _found(file_id, "March.pdf", "PDF")

    async def children(session, folder_id, limit):
        raise AssertionError("a file has nothing inside it to list")

    monkeypatch.setattr(gdrive, "describe", describe)
    monkeypatch.setattr(gdrive, "children", children)

    with pytest.raises(ToolError, match="not a folder"):
        await google.drive_list_folder(None, folder_id="p-1")
    assert shown == []


async def test_reading_a_folder_points_to_listing_it(shown, monkeypatch):
    async def read(session, file_id, *, sheets_as_text=True):
        raise gdrive.UnreadableFileError("folder")

    monkeypatch.setattr(gdrive, "read", read)

    with pytest.raises(ToolError, match="drive_list_folder"):
        await google.drive_read(None, "f-1")


SHEET = "docs.google.com/spreadsheets/d/1xQ7vTn3PzKc0aWm_Ye8dRhJ5uLfB2sGoN4/edit?usp=drivesdk"
FOLDER_LINK = "drive.google.com/drive/folders/1bF3kPq9ZsUw7yRt2LmVx0NcA8dEhJo5G"


@pytest.mark.parametrize(
    ("written", "sent"),
    [
        # Written the way an address is said: completed.
        (f"Here is the link: {SHEET}", f"Here is the link: https://{SHEET}"),
        (f"{FOLDER_LINK}\nThanks", f"https://{FOLDER_LINK}\nThanks"),
        (
            f"See ({SHEET}) and {FOLDER_LINK}.",
            f"See (https://{SHEET}) and https://{FOLDER_LINK}.",
        ),
        # Already whole: left as it is.
        (f"Here it is: https://{SHEET}", f"Here it is: https://{SHEET}"),
        (f"<https://{FOLDER_LINK}>", f"<https://{FOLDER_LINK}>"),
        # Not a link to anything in Drive.
        ("Mail me at asha@docs.google.com/x", "Mail me at asha@docs.google.com/x"),
        (
            "notdocs.google.com/abc and example.com/docs",
            "notdocs.google.com/abc and example.com/docs",
        ),
        ("No links here.", "No links here."),
    ],
)
def test_a_drive_link_in_a_draft_is_made_whole(written, sent):
    assert whole_links(written) == sent


async def test_an_email_is_drafted_with_its_link_whole(monkeypatch):
    proposed: list[dict] = []

    class Gate:
        def propose(self, kind, payload, preview):
            proposed.append(payload)
            return SimpleNamespace(id="act-1", status="drafted")

    monkeypatch.setattr(actions, "_require_write", lambda service: None)
    context = SimpleNamespace(userdata=SimpleNamespace(gate=Gate()))

    drafted = await actions.gmail_draft_email(
        context, ["asha@example.com"], "Q3 budget", f"Here is the link: {SHEET}"
    )

    assert proposed[0]["body"] == f"Here is the link: https://{SHEET}"
    # What the user hears read back is what will be sent.
    assert drafted["draft"]["body"] == proposed[0]["body"]
