from api.deck import (
    awaits_reply,
    deck,
    deck_event,
    email_item,
    invitation_item,
    unanswered,
)
from api.mail import mail_item


def message(sender: str, subject: str) -> dict:
    return {
        "id": "18f2a9c4d7e1b360",
        "internalDate": "1791030000000",
        "snippet": "Payment is due by the 15th &amp; not later.",
        "payload": {
            "headers": [
                {"name": "From", "value": sender},
                {"name": "Subject", "value": subject},
            ]
        },
    }


def event(**overrides) -> dict:
    return {
        "id": "evt1",
        "summary": "Design review",
        "start": {"dateTime": "2026-10-05T14:00:00+05:30"},
        "end": {"dateTime": "2026-10-05T14:45:00+05:30"},
        **overrides,
    }


def test_email_item_names_the_sender_and_unescapes_the_snippet():
    item = email_item(
        mail_item(message("Asha Verma <asha@example.com>", "Q3 invoice"), None)
    )

    assert item.kind == "email"
    assert item.title == "Asha Verma: Q3 invoice"
    assert item.detail == "Payment is due by the 15th & not later."
    assert item.gist is None
    assert not item.has_draft


def test_email_item_falls_back_to_the_address_and_a_placeholder_subject():
    item = email_item(mail_item(message("asha@example.com", ""), None))

    assert item.title == "asha@example.com: (no subject)"


def test_email_item_carries_orbits_gist_and_whether_a_reply_is_drafted():
    record = {
        "gist": "Asha wants the invoice paid.",
        "needs_reply": 1,
        "reply": "Hi Asha",
        "status": "suggested",
    }
    item = email_item(
        mail_item(message("Asha Verma <asha@example.com>", "Q3 invoice"), record)
    )

    assert item.gist == "Asha wants the invoice paid."
    assert item.has_draft
    assert item.message_id == "18f2a9c4d7e1b360"


def test_only_unanswered_invitations_wait_on_the_user():
    def invited(response):
        return event(
            attendees=[
                {"email": "priya@example.com", "responseStatus": "accepted"},
                {"email": "me@example.com", "self": True, "responseStatus": response},
            ]
        )

    assert awaits_reply(invited("needsAction"))
    assert not awaits_reply(invited("accepted"))
    # An event with no attendees is the user's own; nobody is waiting on a reply.
    assert not awaits_reply(event())


def test_invitation_item_says_who_invited_and_when_it_starts():
    item = invitation_item(event(organizer={"displayName": "Priya Raman"}))

    assert item.kind == "invitation"
    assert item.title == "Design review"
    assert "Priya Raman" in item.detail
    assert item.at == "2026-10-05T14:00:00+05:30"


def test_timed_event_keeps_its_times_and_meeting_place():
    mapped = deck_event(event(hangoutLink="https://meet.google.com/abc"))

    assert not mapped.all_day
    assert mapped.start == "2026-10-05T14:00:00+05:30"
    assert mapped.place == "Google Meet"


def test_all_day_event_is_dated_not_timed():
    mapped = deck_event(
        event(
            start={"date": "2026-10-05"}, end={"date": "2026-10-06"}, location="Hall 2"
        )
    )

    assert mapped.all_day
    assert mapped.start == "2026-10-05"
    assert mapped.place == "Hall 2"


async def test_deck_is_empty_and_marked_unconnected_without_a_sign_in(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ORBIT_SECRETS_DIR", str(tmp_path))
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)

    result = await deck()

    assert not result.connected
    assert result.waiting == []
    assert result.events == []


def test_a_recurring_invitation_waits_once_not_per_occurrence():
    me = [{"email": "me@example.com", "self": True, "responseStatus": "needsAction"}]
    weekly = [
        event(id=f"class_{day}", recurringEventId="class", attendees=me)
        for day in ("mon", "wed", "fri")
    ]
    one_off = event(id="review", attendees=me)

    waiting = unanswered([*weekly, one_off])

    assert [e["id"] for e in waiting] == ["class_mon", "review"]
