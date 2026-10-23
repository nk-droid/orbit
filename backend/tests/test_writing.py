import base64
from email import message_from_bytes, policy

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from livekit.agents.llm import ToolError

import writing
from actions.effects import perform
from actions.gate import ActionGate
from agent_tools import actions, stage
from integrations.google import api, gdrive, gmail

LIST = "Bring:\n\n- passport\n- **charger**\n\nBest,\nNidhish"
LIST_HTML = (
    "<p>Bring:</p>\n<ul>\n<li>passport</li>\n<li><strong>charger</strong></li>\n"
    "</ul>\n<p>Best,<br>\nNidhish</p>"
)
LIST_WORDS = "Bring:\n\n- passport\n- charger\n\nBest,\nNidhish"


# --- Markdown and HTML become one clean kind of HTML -------------------------


def test_markdown_is_drawn_with_its_lists_tables_and_emphasis():
    drawn = writing.to_html(
        "# Plan\n\n1. one\n2. two\n\n| Stage | Share |\n|---|---|\n| Canary | 1% |\n\n"
        "~~old~~ `code` [tracker](https://example.com/t)",
        "markdown",
    )

    for piece in (
        "<h1>Plan</h1>",
        "<ol>\n<li>one</li>",
        "<th>Stage</th>",
        "<td>1%</td>",
        "<s>old</s>",
        "<code>code</code>",
        '<a href="https://example.com/t">tracker</a>',
    ):
        assert piece in drawn


def test_the_end_of_a_line_ends_the_line_in_what_orbit_writes():
    assert writing.to_html("Best,\nNidhish", "markdown") == "<p>Best,<br>\nNidhish</p>"
    # A Markdown file is read the usual way: its lines run on.
    assert (
        writing.to_html("Best,\nNidhish", "markdown", breaks=False)
        == "<p>Best,\nNidhish</p>"
    )


def test_a_ticked_box_in_markdown_is_drawn_as_one():
    drawn = writing.to_html("- [x] booked\n- [ ] paid", "markdown")

    assert "<li>☑ booked</li>" in drawn
    assert "<li>☐ paid</li>" in drawn


def test_line_ends_a_model_escaped_twice_are_taken_as_line_ends():
    twice = "| Planet | Moons |\\n|---|---|\\n| Earth | 1 |"

    assert writing.as_meant(twice) == "| Planet | Moons |\n|---|---|\n| Earth | 1 |"
    assert "<td>Earth</td>" in writing.to_html(writing.as_meant(twice), "markdown")
    # Text with real line ends is left alone, backslashes and all.
    code = "Run this:\nprint('a\\nb')"
    assert writing.as_meant(code) == code
    assert writing.as_meant("One line.") == "One line."


def test_plain_text_has_no_formatting():
    assert writing.to_html("Just **words**.", "plain") is None


def test_html_written_inside_markdown_is_shown_not_used():
    drawn = writing.to_html("Hello <script>alert(1)</script> <b>there</b>", "markdown")

    assert "<script>" not in drawn and "<b>" not in drawn
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in drawn


@pytest.mark.parametrize(
    ("written", "kept"),
    [
        # What a page would run, and what would be fetched from elsewhere.
        ('<p onclick="x()">hi</p><script>alert(1)</script>', "<p>hi</p>"),
        ("<style>p{display:none}</style><p>seen</p>", "<p>seen</p>"),
        ('<iframe src="https://e.example"></iframe>ok', "ok"),
        ('<form action="https://e.example"><input name=q>go</form>ok', "ok"),
        ('<meta http-equiv="refresh" content="0;url=https://e.example">ok', "ok"),
        ("<svg><script>x</script><text>t</text></svg>after", "after"),
        ('<img src="https://t.example/p.gif" alt="A chart">', "[Picture: A chart]"),
        ('<img src=x onerror="alert(1)">', ""),
        # Nothing can be hidden from the reader by how it is styled.
        ('<div style="display:none">read me</div>', "<div>read me</div>"),
        ('<span style="font-size:0">tiny</span>', "tiny"),
        ("<font color=white>white on white</font>", "white on white"),
        # Tags a writer left open are closed the way a browser would close them.
        ("<ul><li>one<li>two</ul>", "<ul><li>one</li><li>two</li></ul>"),
        ("<p>one<p>two", "<p>one</p><p>two</p>"),
        (
            "<table><tr><td>a<td>b<tr><td>c</table>",
            "<table><tr><td>a</td><td>b</td></tr><tr><td>c</td></tr></table>",
        ),
        ("</div>stray <b>bold", "stray <b>bold</b>"),
        # Words are kept as words.
        ("5 &lt; 6 &amp; <b>7 > 2</b>", "5 &lt; 6 &amp; <b>7 &gt; 2</b>"),
        (
            "<section><h2>T</h2><article>x</article></section>",
            "<div><h2>T</h2><div>x</div></div>",
        ),
        (
            '<td colspan="2" rowspan="9999" bgcolor="red">x</td>',
            '<td colspan="2">x</td>',
        ),
        ('<ol start="3" type="a"><li>c</ol>', '<ol start="3"><li>c</li></ol>'),
    ],
)
def test_html_is_written_out_again_with_only_what_is_kept(written, kept):
    assert writing.clean(written) == kept


@pytest.mark.parametrize(
    "address",
    [
        "javascript:alert(1)",
        " JaVa\tScRiPt:alert(1)",
        "java\nscript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "vbscript:msgbox(1)",
        "//evil.example/x",
        "/api/mail/1/draft/send",
        "file:///etc/passwd",
    ],
)
def test_a_link_that_does_not_lead_to_the_web_is_only_its_words(address):
    assert writing.clean(f'<a href="{address}">press</a>') == "press"


def test_a_link_to_the_web_or_an_email_address_is_kept_and_nothing_else_on_it():
    assert (
        writing.clean(
            '<a href="https://ok.example/?q=<&x=\'" onmouseover="x()" target="_top">go</a>'
        )
        == '<a href="https://ok.example/?q=&lt;&amp;x=&#x27;">go</a>'
    )
    assert (
        writing.clean('<a href="mailto:a@b.co">a</a>')
        == '<a href="mailto:a@b.co">a</a>'
    )


def test_cleaning_what_is_already_clean_changes_nothing():
    assert writing.clean(LIST_HTML) == LIST_HTML
    assert writing.clean(writing.clean("<ul><li>one<li><b>two")) == (
        "<ul><li>one</li><li><b>two</b></li></ul>"
    )


def test_the_words_of_formatted_text_read_as_a_person_would_read_them():
    drawn = writing.to_html(
        "# Trip\n\nHi **Asha**,\n\n1. book\n2. pack\n   - light\n\n"
        "| Option | Cost |\n|---|---|\n| Train | 900 |\n| Flight | 4,500 |\n\n"
        "See the [sheet](https://docs.google.com/x) or write to <a@b.co>.\n\nBest,\nNidhish",
        "markdown",
    )

    assert writing.words(drawn) == (
        "Trip\n\nHi Asha,\n\n1. book\n2. pack\n  - light\n\n"
        "Option | Cost\nTrain | 900\nFlight | 4,500\n\n"
        "See the sheet (https://docs.google.com/x) or write to a@b.co.\n\nBest,\nNidhish"
    )


def test_a_page_is_a_whole_document_with_its_title_made_safe():
    page = writing.page("Q3 <plan>", "<p>x</p>")

    assert page.startswith("<!doctype html>")
    assert "<title>Q3 &lt;plan&gt;</title>" in page
    assert "<body>\n<p>x</p>\n</body>" in page


# --- An email written with formatting ----------------------------------------


def _parts(raw: str):
    email = message_from_bytes(base64.urlsafe_b64decode(raw), policy=policy.default)
    return email, {
        part.get_content_type(): part.get_content()
        for part in email.walk()
        if not part.is_multipart()
    }


def test_a_formatted_email_carries_its_words_and_its_html():
    raw = gmail.build_raw(
        ["asha@example.com"],
        "Packing",
        LIST_WORDS,
        html=writing.page("Packing", LIST_HTML),
    )

    email, parts = _parts(raw)

    assert email.get_content_type() == "multipart/alternative"
    assert parts["text/plain"].strip() == LIST_WORDS
    assert "<li><strong>charger</strong></li>" in parts["text/html"]
    # The formatted one comes last, which is the one mail programs prefer.
    assert [p.get_content_type() for p in email.iter_parts()] == [
        "text/plain",
        "text/html",
    ]


def test_a_plain_email_is_sent_as_it_always_was():
    email, parts = _parts(gmail.build_raw(["asha@example.com"], "Hi", "Hello."))

    assert email.get_content_type() == "text/plain"
    assert list(parts) == ["text/plain"]


@pytest.fixture
def sent(monkeypatch) -> list[dict]:
    """Whatever is sent through Gmail is kept here instead."""
    out: list[dict] = []

    async def send(session, raw, thread_id=None):
        email, parts = _parts(raw)
        out.append({"email": email, "parts": parts, "thread_id": thread_id})
        return {"id": "sent-1", "thread_id": thread_id}

    monkeypatch.setattr(gmail, "send", send)
    return out


async def test_an_approved_formatted_email_goes_out_as_the_html_that_was_shown(sent):
    payload = {
        "to": ["asha@example.com"],
        "subject": "Packing",
        "body": LIST_WORDS,
        "html": LIST_HTML,
    }

    await perform(None, "gmail.send_email", payload)

    (message,) = sent
    assert LIST_HTML in message["parts"]["text/html"]
    assert message["parts"]["text/plain"].strip() == LIST_WORDS


# --- A document or a file written with formatting ----------------------------


@pytest.fixture
def uploads(monkeypatch):
    """A stand-in for Drive's upload that keeps what each upload carried."""
    received: list[dict] = []

    async def upload(request: web.Request) -> web.Response:
        parts = await request.multipart()
        described = await (await parts.next()).json()
        content = await parts.next()
        received.append(
            {
                "described": described,
                "type": content.headers["Content-Type"],
                "content": await content.text(),
            }
        )
        return web.json_response({"id": "f-1", "name": described["name"]})

    async def no_token() -> dict[str, str]:
        return {}

    monkeypatch.setattr(api, "_headers", no_token)
    return received, upload


async def _with_drive(uploads, monkeypatch, act):
    received, upload = uploads
    app = web.Application()
    app.router.add_post("/upload", upload)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        monkeypatch.setattr(gdrive, "_UPLOAD", str(server.make_url("/upload")))
        await act(session)
    return received


async def test_a_formatted_document_is_made_from_its_html(uploads, monkeypatch):
    payload = {"title": "Packing", "content": LIST_WORDS, "html": LIST_HTML}

    (made,) = await _with_drive(
        uploads,
        monkeypatch,
        lambda session: perform(session, "drive.create_document", payload),
    )

    # Drive turns the page into a Google Doc, which keeps the list and the bold.
    assert made["described"] == {
        "name": "Packing",
        "mimeType": "application/vnd.google-apps.document",
    }
    assert made["type"].startswith("text/html")
    assert made["content"].startswith("<!doctype html>")
    assert LIST_HTML in made["content"]


async def test_a_plain_document_is_made_from_its_text(uploads, monkeypatch):
    payload = {"title": "Packing", "content": "Passport", "html": None}

    (made,) = await _with_drive(
        uploads,
        monkeypatch,
        lambda session: perform(session, "drive.create_document", payload),
    )

    assert made["type"].startswith("text/plain")
    assert made["content"] == "Passport"


async def test_a_file_kept_as_written_is_not_turned_into_a_google_doc(
    uploads, monkeypatch
):
    payload = {"name": "notes.md", "content": LIST, "mime_type": "text/markdown"}

    (made,) = await _with_drive(
        uploads,
        monkeypatch,
        lambda session: perform(session, "drive.create_file", payload),
    )

    assert made["described"] == {"name": "notes.md", "mimeType": "text/markdown"}
    assert made["type"].startswith("text/markdown")
    assert made["content"] == LIST


# --- What the drafting tools make of what the model wrote --------------------


class _Context:
    def __init__(self) -> None:
        self.userdata = type("State", (), {"gate": ActionGate(), "handlers": {}})()


@pytest.fixture
def allowed(monkeypatch):
    monkeypatch.setattr(actions, "_require_write", lambda service: None)
    monkeypatch.setattr(actions, "_http", lambda: None)


def _drafted(context: _Context):
    (action,) = context.userdata.gate._actions.values()
    return action


async def test_a_formatted_email_is_drafted_as_what_will_be_sent(allowed):
    context = _Context()

    told = await actions.gmail_draft_email(
        context, ["asha@example.com"], "Packing", LIST, "markdown"
    )

    action = _drafted(context)
    # What is sent is the clean HTML, with its words for programs that show none.
    assert action.payload == {
        "to": ["asha@example.com"],
        "subject": "Packing",
        "body": LIST_WORDS,
        "html": LIST_HTML,
    }
    # The card draws that same HTML.
    assert action.preview["html"] == LIST_HTML
    assert action.preview["format"] == "Markdown"
    # The model is given the words to read out, and never the markup.
    assert told["draft"] == {
        "what": "Send new email",
        "to": ["asha@example.com"],
        "subject": "Packing",
        "body": LIST_WORDS,
        "format": "Markdown",
    }
    assert "never say a formatting symbol" in told["formatting"]


async def test_the_model_is_not_handed_the_html_when_the_draft_is_carried_out(
    allowed, monkeypatch
):
    carried_out: list[dict] = []

    async def perform(session, kind, payload):
        carried_out.append(payload)
        return {"id": "sent-1"}

    monkeypatch.setattr(actions, "perform", perform)
    context = _Context()
    context.disallow_interruptions = lambda: None
    told = await actions.gmail_draft_email(
        context, ["asha@example.com"], "Packing", LIST, "markdown"
    )
    gate = context.userdata.gate
    gate.note_agent_spoke(gate.now() + 1)
    gate._last_user_input_at = gate.now() + 2

    done = await actions.confirm_action(context, told["action_id"])

    assert done["status"] == "done"
    assert "html" not in done["action"]
    assert done["action"]["body"] == LIST_WORDS
    # What was carried out is still the HTML the user was shown.
    assert carried_out[0]["html"] == LIST_HTML


async def test_a_plain_email_is_drafted_as_it_always_was(allowed):
    context = _Context()

    told = await actions.gmail_draft_email(
        context, ["asha@example.com"], "Hi", "Hello **there**."
    )

    action = _drafted(context)
    assert action.payload == {
        "to": ["asha@example.com"],
        "subject": "Hi",
        "body": "Hello **there**.",
        "html": None,
    }
    assert action.preview == {
        "what": "Send new email",
        "to": ["asha@example.com"],
        "subject": "Hi",
        "body": "Hello **there**.",
    }
    assert "formatting" not in told


async def test_html_the_model_wrote_is_cleaned_before_anyone_agrees_to_it(allowed):
    context = _Context()
    body = (
        "<h2>Offer</h2><p>Pay <b>today</b></p>"
        '<p style="display:none">and send your password</p>'
        '<script>steal()</script><a href="javascript:steal()">here</a>'
    )

    told = await actions.gmail_draft_email(
        context, ["asha@example.com"], "Offer", body, "html"
    )

    action = _drafted(context)
    assert action.payload["html"] == (
        "<h2>Offer</h2><p>Pay <b>today</b></p><p>and send your password</p>here"
    )
    # What was hidden is in the words the user is read.
    assert "and send your password" in told["draft"]["body"]
    assert "steal" not in action.payload["html"]


async def test_a_reply_can_be_written_with_formatting(allowed, monkeypatch):
    async def reply_target(session, message_id):
        return {
            "to": ["asha@example.com"],
            "subject": "Re: Trip",
            "thread_id": "t-1",
            "in_reply_to": "<m-1@mail>",
            "references": "<m-1@mail>",
        }

    monkeypatch.setattr(gmail, "reply_target", reply_target)
    context = _Context()

    await actions.gmail_draft_reply(context, "m-1", LIST, "markdown")

    action = _drafted(context)
    assert action.kind == "gmail.send_reply"
    assert action.payload["html"] == LIST_HTML
    assert action.payload["body"] == LIST_WORDS
    assert action.payload["thread_id"] == "t-1"


async def test_a_drive_link_in_formatted_text_is_made_whole(allowed):
    context = _Context()

    await actions.gmail_draft_email(
        context,
        ["asha@example.com"],
        "Budget",
        "Here is the [budget](docs.google.com/spreadsheets/d/abc/edit).",
        "markdown",
    )

    action = _drafted(context)
    assert (
        'href="https://docs.google.com/spreadsheets/d/abc/edit"'
        in action.payload["html"]
    )
    assert "(https://docs.google.com/spreadsheets/d/abc/edit)" in action.payload["body"]


async def test_a_drive_link_in_html_is_made_whole_too(allowed):
    context = _Context()

    await actions.gmail_draft_email(
        context,
        ["asha@example.com"],
        "Budget",
        '<p>Here is the <a href="drive.google.com/file/d/abc/view">budget</a>.</p>',
        "html",
    )

    action = _drafted(context)
    assert action.payload["html"] == (
        '<p>Here is the <a href="https://drive.google.com/file/d/abc/view">budget</a>.</p>'
    )


async def test_a_formatted_document_is_drafted_as_a_google_doc(allowed):
    context = _Context()

    told = await actions.drive_draft_document(context, "Packing", LIST, "markdown")

    action = _drafted(context)
    assert action.kind == "drive.create_document"
    assert action.payload == {
        "title": "Packing",
        "content": LIST_WORDS,
        "html": LIST_HTML,
    }
    assert told["draft"]["what"] == "Create Google Doc"
    assert "html" not in told["draft"]


@pytest.mark.parametrize(
    ("written_in", "title", "name", "what", "mime_type"),
    [
        (
            "markdown",
            "Trip notes",
            "Trip notes.md",
            "Create Markdown file",
            "text/markdown",
        ),
        ("markdown", "notes.MD", "notes.MD", "Create Markdown file", "text/markdown"),
        ("html", "Trip page", "Trip page.html", "Create HTML file", "text/html"),
        ("plain", "Trip", "Trip.txt", "Create text file", "text/plain"),
    ],
)
async def test_a_file_asked_for_as_a_file_is_drafted_as_one(
    allowed, written_in, title, name, what, mime_type
):
    context = _Context()

    told = await actions.drive_draft_document(
        context, title, "# Trip\n\nBook the train.", written_in, True
    )

    action = _drafted(context)
    assert action.kind == "drive.create_file"
    assert action.payload["name"] == name
    assert action.payload["mime_type"] == mime_type
    assert told["draft"]["what"] == what
    assert told["draft"]["name"] == name


async def test_a_markdown_file_keeps_what_was_written_and_is_shown_as_it_will_read(
    allowed,
):
    context = _Context()

    await actions.drive_draft_document(context, "Notes", LIST, "markdown", True)

    action = _drafted(context)
    assert action.payload["content"] == LIST
    # Drawn as a Markdown file is read, where the end of a line does not end it.
    assert "<br>" not in action.preview["html"]
    assert "<li><strong>charger</strong></li>" in action.preview["html"]


async def test_an_html_file_is_the_clean_page_the_user_was_shown(allowed):
    context = _Context()

    await actions.drive_draft_document(
        context,
        "Trip page",
        "<h1>Trip</h1><script>x()</script><p onclick='x()'>Book it.</p>",
        "html",
        True,
    )

    action = _drafted(context)
    assert action.preview["html"] == "<h1>Trip</h1><p>Book it.</p>"
    assert action.payload["content"] == writing.page(
        "Trip page", "<h1>Trip</h1><p>Book it.</p>"
    )


async def test_markup_with_no_words_in_it_is_not_drafted(allowed):
    context = _Context()

    with pytest.raises(ToolError, match="no words"):
        await actions.gmail_draft_email(
            context, ["asha@example.com"], "Hi", "<script>x()</script>", "html"
        )
    assert context.userdata.gate._actions == {}


# --- Something Orbit writes for the screen ------------------------------------


@pytest.fixture
def shown(monkeypatch) -> list[dict]:
    cards: list[dict] = []

    async def show(card, *, push=None):
        cards.append(card)

    monkeypatch.setattr(stage, "show", show)
    return cards


async def test_a_list_whose_line_ends_were_escaped_twice_is_still_a_list(
    shown, allowed
):
    await stage.write_on_screen(
        None, "Steps", "1. Grind the beans.\\n2. Pour the water."
    )
    context = _Context()
    await actions.gmail_draft_email(
        context, ["asha@example.com"], "Steps", "Hi,\\n\\n- one\\n- two", "markdown"
    )

    (card,) = shown
    assert card["html"].count("<li>") == 2
    action = _drafted(context)
    assert action.payload["html"].count("<li>") == 2
    assert action.payload["body"] == "Hi,\n\n- one\n- two"


async def test_what_orbit_writes_for_the_screen_arrives_as_clean_html(shown):
    answer = await stage.write_on_screen(
        None, "Trip options", "| Option | Cost |\n|---|---|\n| Train | 900 |"
    )

    (card,) = shown
    assert card["id"] == "note:trip-options"
    assert (card["kind"], card["title"]) == ("note", "Trip options")
    assert "<th>Option</th>" in card["html"] and "<td>900</td>" in card["html"]
    assert answer == {"on_screen": "Trip options"}


async def test_the_same_title_again_is_the_same_card(shown):
    await stage.write_on_screen(None, "Trip options!", "one")
    await stage.write_on_screen(None, "trip  options", "two")

    assert [card["id"] for card in shown] == ["note:trip-options", "note:trip-options"]


async def test_html_for_the_screen_is_cleaned_like_any_other(shown):
    await stage.write_on_screen(
        None,
        "Page",
        "<h1>Hi</h1><script>steal()</script><img src=x onerror=x()>",
        "html",
    )

    (card,) = shown
    assert card["html"] == "<h1>Hi</h1>"


async def test_nothing_or_too_much_is_not_put_on_the_screen(shown):
    with pytest.raises(ToolError, match="nothing to show"):
        await stage.write_on_screen(None, "Empty", "   ")
    with pytest.raises(ToolError, match="too long"):
        await stage.write_on_screen(None, "Book", "x" * (stage.MAX_WRITTEN_CHARS + 1))

    assert shown == []
