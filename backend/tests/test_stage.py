import json
import time
from types import SimpleNamespace

import pytest
from livekit.agents.llm import ToolError

from agent_tools import browser, google, reminders, stage, standby, web
from agent_tools.reminders import reminder_add, reminder_list
from agent_tools.web import web_search
from api.mail import Waiting


@pytest.fixture
def shown(monkeypatch) -> list[dict]:
    """What the tools asked the app to put on screen."""
    asked: list[dict] = []

    async def show(card, *, push=None):
        asked.append({"card": card, "push": push})

    monkeypatch.setattr(stage, "show", show)
    return asked


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("ORBIT_DATA_DIR", str(tmp_path))


async def test_a_card_reaches_the_app_as_a_text_stream(monkeypatch):
    sent: list[tuple[str, str]] = []

    async def send_text(text, *, topic):
        sent.append((topic, text))

    room = SimpleNamespace(local_participant=SimpleNamespace(send_text=send_text))
    monkeypatch.setattr(
        stage, "get_job_context", lambda required=False: SimpleNamespace(room=room)
    )

    await stage.show(
        {"id": "browser", "kind": "browser"}, push={"steps": "Opened a page"}
    )

    ((topic, text),) = sent
    assert topic == stage.TOPIC
    assert json.loads(text) == {
        "type": "show",
        "card": {"id": "browser", "kind": "browser"},
        "push": {"steps": "Opened a page"},
    }


async def test_the_app_is_told_before_orbit_stands_by(monkeypatch):
    sent: list[tuple[str, str]] = []

    async def send_text(text, *, topic):
        sent.append((topic, text))

    room = SimpleNamespace(local_participant=SimpleNamespace(send_text=send_text))
    monkeypatch.setattr(
        stage, "get_job_context", lambda required=False: SimpleNamespace(room=room)
    )

    # What the standby tool runs when the model calls it, before any last words.
    await standby._tell_app(None)

    ((topic, text),) = sent
    assert topic == stage.TOPIC
    assert json.loads(text) == {"type": "standby"}


async def test_outside_a_room_nothing_is_sent_and_nothing_fails():
    await stage.show({"id": "needs", "kind": "needs"})


async def test_a_failure_to_reach_the_app_never_fails_the_tool(monkeypatch):
    async def send_text(text, *, topic):
        raise ConnectionError("room closed")

    room = SimpleNamespace(local_participant=SimpleNamespace(send_text=send_text))
    monkeypatch.setattr(
        stage, "get_job_context", lambda required=False: SimpleNamespace(room=room)
    )

    await stage.show({"id": "needs", "kind": "needs"})


async def test_a_list_card_is_kept_short(shown):
    rows = [stage.row(f"Result {n}", detail="x" * 900) for n in range(20)]

    await stage.show_list("found:web", "Web", "globe", rows, tag="query")

    card = shown[0]["card"]
    assert len(card["rows"]) == stage.MAX_ROWS
    assert card["more"] == 20 - stage.MAX_ROWS
    assert len(card["rows"][0]["detail"]) == stage.MAX_FIELD_CHARS
    assert "line" not in card["rows"][0]


async def test_a_mail_search_is_put_on_screen(shown, monkeypatch):
    async def search(session, query, limit):
        return [
            {
                "id": "m1",
                "from": "Asha Verma <asha@example.com>",
                "subject": "Q3 invoice",
                "received": "Sat 03 Oct 17:58",
                "unread": True,
                "snippet": "Payment is due by the 15th.",
            }
        ]

    monkeypatch.setattr(google.gmail, "search", search)
    monkeypatch.setattr(google.utils.http_context, "http_session", lambda: None)

    await google.gmail_search(None, "is:unread")

    card = shown[0]["card"]
    assert (card["id"], card["kind"], card["title"]) == ("found:mail", "list", "Mail")
    assert card["rows"] == [
        {
            "title": "Asha Verma",
            "line": "Q3 invoice",
            "detail": "Payment is due by the 15th.",
            "meta": "Sat 03 Oct 17:58",
        }
    ]


async def test_reading_an_email_opens_it_on_screen(shown, monkeypatch):
    async def read(session, message_id):
        return {"id": message_id, "subject": "Q3 invoice", "body": "…"}

    monkeypatch.setattr(google.gmail, "read", read)
    monkeypatch.setattr(google.utils.http_context, "http_session", lambda: None)

    await google.gmail_read(None, "m1")

    assert shown[0]["card"] == {"id": "mail:m1", "kind": "mail", "message_id": "m1"}


async def test_reminder_tools_bring_up_the_reminders_card(shown):
    await reminder_add(None, "Check the oven", in_minutes=20)
    await reminder_list(None)

    assert [s["card"] for s in shown] == [reminders.REMINDERS_CARD] * 2


async def test_a_web_search_is_put_on_screen(shown, monkeypatch):
    async def search(query, *, news, limit):
        return [
            {
                "title": "Russell wins in Azerbaijan",
                "site": "Example News",
                "url": "https://news.example.com/f1",
                "snippet": "George Russell wins.",
                "date": "2026-09-28",
            }
        ]

    monkeypatch.setattr(web.duckduckgo, "search", search)

    await web_search(None, "Formula 1 result", news=True)

    card = shown[0]["card"]
    assert card["title"] == "Web"
    assert card["rows"][0]["line"] == "Example News · 2026-09-28"


async def test_what_is_waiting_matches_what_the_app_shows(shown, monkeypatch):
    async def waiting_mail(session):
        return Waiting(items=[], reading=False, trouble=None)

    async def raw_events(session, start, end, limit=20):
        return [
            {
                "id": "evt1",
                "summary": "Project sync",
                "start": {"dateTime": "2026-10-06T15:00:00+05:30"},
                "end": {"dateTime": "2026-10-06T15:30:00+05:30"},
                "organizer": {"email": "rahul@example.com"},
                "attendees": [{"self": True, "responseStatus": "needsAction"}],
            }
        ]

    monkeypatch.setattr(google, "waiting_mail", waiting_mail)
    monkeypatch.setattr(google.gcal, "raw_events", raw_events)
    monkeypatch.setattr(google.utils.http_context, "http_session", lambda: None)
    google.reminders.add("Pay the bill", time.time() - 60)
    google.reminders.add("Later", time.time() + 3600)

    waiting = await google.waiting_on_me(None)

    assert shown[0]["card"] == {"id": "needs", "kind": "needs"}
    assert [i["title"] for i in waiting["invitations_not_answered"]] == ["Project sync"]
    assert [r["title"] for r in waiting["reminders_due"]] == ["Pay the bill"]


@pytest.mark.parametrize(
    ("name", "arguments", "step"),
    [
        (
            "browser_navigate",
            {"url": "https://example.com/a?b=1"},
            "Opened example.com",
        ),
        ("browser_click", {"element": "Register button"}, "Clicked Register button"),
        (
            "browser_type",
            {"element": "Search box", "text": "secret"},
            "Typed into Search box",
        ),
        ("browser_snapshot", {}, "Read the page"),
        (
            "browser_tabs",
            {"action": "new", "url": "https://example.org/docs"},
            "Opened a new tab on example.org",
        ),
        ("browser_tabs", {"action": "new"}, "Opened a new tab"),
        ("browser_tabs", {"action": "select", "index": 1}, "Switched to another tab"),
        ("browser_tabs", {"action": "close"}, "Closed a tab"),
        ("browser_tabs", {"action": "list"}, "Looked at the open tabs"),
        ("browser_close", {}, "Closed the browser"),
    ],
)
def test_a_browser_step_says_what_was_done_but_never_what_was_typed(
    name, arguments, step
):
    assert browser._step(name, arguments) == step


class AppOnTheOtherEnd:
    """Stands in for the browser: records what it was asked and gives an answer."""

    def __init__(self, monkeypatch, answer: dict | Exception) -> None:
        self.asked: list[dict] = []
        self._answer = answer
        participant = SimpleNamespace(perform_rpc=self._perform_rpc)
        room = SimpleNamespace(local_participant=participant)
        monkeypatch.setattr(
            stage, "get_job_context", lambda required=False: SimpleNamespace(room=room)
        )
        user = SimpleNamespace(identity="user-1")
        self.context = SimpleNamespace(
            session=SimpleNamespace(room_io=SimpleNamespace(linked_participant=user))
        )

    async def _perform_rpc(
        self, *, destination_identity, method, payload, response_timeout
    ):
        self.asked.append(
            {"to": destination_identity, "method": method, **json.loads(payload)}
        )
        if isinstance(self._answer, Exception):
            raise self._answer
        return json.dumps(self._answer)


async def test_the_agent_asks_the_app_to_move_cards_and_hears_what_changed(monkeypatch):
    app = AppOnTheOtherEnd(
        monkeypatch,
        {
            "changed": ["Reminders"],
            "cards": [{"id": "reminders", "title": "Reminders", "where": "background"}],
        },
    )

    result = await stage.arrange_cards(app.context, "background", "reminders")

    assert app.asked == [
        {
            "to": "user-1",
            "method": stage.CARDS_METHOD,
            "action": "background",
            "which": "reminders",
        }
    ]
    assert result["changed"] == ["Reminders"]
    assert "note" not in result


async def test_the_agent_is_told_when_no_card_matched(monkeypatch):
    cards = [{"id": "needs", "title": "Needs you", "where": "front"}]
    app = AppOnTheOtherEnd(monkeypatch, {"changed": [], "cards": cards})

    result = await stage.arrange_cards(app.context, "close", "invoices")

    assert "nothing changed" in result["note"]
    assert result["cards"] == cards


async def test_listing_the_cards_is_not_reported_as_a_miss(monkeypatch):
    app = AppOnTheOtherEnd(monkeypatch, {"changed": [], "cards": []})

    assert "note" not in await stage.arrange_cards(app.context, "list")


async def test_a_screen_that_does_not_answer_is_reported(monkeypatch):
    app = AppOnTheOtherEnd(monkeypatch, TimeoutError("no answer"))

    with pytest.raises(ToolError, match="nothing was moved or closed"):
        await stage.arrange_cards(app.context, "background", "all")


async def test_without_a_screen_the_agent_says_so():
    nobody = SimpleNamespace(
        session=SimpleNamespace(room_io=SimpleNamespace(linked_participant=None))
    )

    with pytest.raises(ToolError, match="cannot be reached"):
        await stage.arrange_cards(nobody, "background", "all")


async def test_a_list_to_glance_at_shows_its_first_entries(shown):
    rows = [stage.row(f"Entry {n}", "x" * 500) for n in range(20)]

    await stage.show_list("found:mail", "Mail", "mail", rows)

    card = shown[-1]["card"]
    assert len(card["rows"]) == stage.MAX_ROWS
    assert card["more"] == 20 - stage.MAX_ROWS
    assert card["whole"] is False
    assert len(card["rows"][0]["line"]) == stage.MAX_FIELD_CHARS


async def test_a_list_to_read_through_shows_all_of_it_in_full(shown):
    written = "A long message.\n" + "It goes on. " * 40
    rows = [stage.row("Asha Verma", written, "", "10:00", whole=True)] * 20

    await stage.show_list("found:chat", "Asha Verma", "chat", rows, whole=True)

    card = shown[-1]["card"]
    assert len(card["rows"]) == 20
    assert card["more"] == 0
    assert card["whole"] is True
    assert card["rows"][0]["line"] == written.strip()
    assert card["rows"][0]["title"] == "Asha Verma"


@pytest.mark.parametrize(
    "address",
    ["javascript:alert(1)", "http://example.com/plain", "file:///etc/hosts", "", None],
)
def test_only_a_secure_web_address_is_offered_as_a_link(address):
    assert "link" not in stage.row("Budget", link=address)
    assert stage.opens(address, "Open") == {}


def test_an_entry_and_a_card_carry_the_link_that_opens_them():
    link = "https://docs.google.com/spreadsheets/d/abc/edit"

    assert stage.row("Budget", "Google Sheet", link=link)["link"] == link
    assert stage.opens(link, "Open in Drive") == {
        "link": link,
        "link_label": "Open in Drive",
    }


async def test_a_list_can_show_every_entry_a_line_each(shown):
    rows = [stage.row(f"File {n}", "x" * 500) for n in range(20)]

    await stage.show_list("found:drive", "Drive", "file", rows, every_row=True)

    card = shown[-1]["card"]
    assert len(card["rows"]) == 20
    assert card["more"] == 0
    # Still a line each: only a list to read through is shown in full.
    assert card["whole"] is False
    assert len(card["rows"][0]["line"]) == stage.MAX_FIELD_CHARS
    assert "link" not in card
