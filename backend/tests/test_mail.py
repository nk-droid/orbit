import pytest
from fastapi import HTTPException

from api import mail
from api.mail import (
    DraftBody,
    discard_draft,
    edit_draft,
    require_app,
    send_draft,
    waiting_mail,
)
from drafting import service, store
from drafting.triage import ModelError, Triage, _in_order
from integrations.google.api import GoogleApiError

AUTH = "https://www.googleapis.com/auth/"
WRITE = {
    "scopes": [AUTH + "gmail.readonly", AUTH + "gmail.compose", AUTH + "gmail.modify"]
}
MESSAGE = "18f2a9c4d7e1b360"
REPLY = "Hi Asha,\n\nFriday works.\n\nNidhish"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("ORBIT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(mail.tokens, "load", lambda: WRITE)
    service._failed.clear()
    mail._fetched.clear()


@pytest.fixture
def sent(monkeypatch) -> list[dict]:
    """Gmail is replaced by a recorder: whatever would be sent is captured instead."""
    outbox: list[dict] = []

    async def reply_target(session, message_id):
        return {
            "to": ["asha@example.com"],
            "subject": "Re: Chapter 3",
            "thread_id": "thread-1",
            "in_reply_to": "<abc@mail.example.com>",
            "references": "<abc@mail.example.com>",
        }

    async def send(session, raw, thread_id=None):
        outbox.append({"raw": raw, "thread_id": thread_id})
        return {"id": "sent-1", "thread_id": thread_id}

    async def answered(session, message_id):
        return False

    monkeypatch.setattr(mail.gmail, "answered", answered)
    monkeypatch.setattr(mail.gmail, "reply_target", reply_target)
    monkeypatch.setattr(mail.gmail, "send", send)
    return outbox


@pytest.fixture
def drafted():
    store.put(
        MESSAGE, Triage(gist="Asha asks about Friday.", needs_reply=True, reply=REPLY)
    )


async def test_the_drafted_reply_is_sent_in_its_thread(drafted, sent):
    result = await send_draft(MESSAGE, DraftBody(body=REPLY))

    assert result.status == "sent"
    assert len(sent) == 1
    assert sent[0]["thread_id"] == "thread-1"
    assert store.get(MESSAGE)["status"] == "sent"


async def test_a_reply_is_sent_only_once(drafted, sent):
    await send_draft(MESSAGE, DraftBody(body=REPLY))

    with pytest.raises(HTTPException) as refused:
        await send_draft(MESSAGE, DraftBody(body=REPLY))

    assert refused.value.status_code == 409
    assert len(sent) == 1


async def test_only_the_text_the_user_saw_is_sent(drafted, sent):
    with pytest.raises(HTTPException) as refused:
        await send_draft(MESSAGE, DraftBody(body="Hi Asha, something else entirely."))

    assert refused.value.status_code == 409
    assert sent == []
    assert store.get(MESSAGE)["status"] == "suggested"


async def test_an_edited_draft_is_what_gets_sent(drafted, sent):
    edited = "Hi Asha,\n\nThursday is better.\n\nNidhish"
    edit_draft(MESSAGE, DraftBody(body=edited))

    await send_draft(MESSAGE, DraftBody(body=edited))

    assert store.get(MESSAGE)["reply"] == edited
    assert len(sent) == 1


async def test_nothing_is_sent_with_read_only_access(drafted, sent, monkeypatch):
    monkeypatch.setattr(
        mail.tokens, "load", lambda: {"scopes": [AUTH + "gmail.readonly"]}
    )

    with pytest.raises(HTTPException) as refused:
        await send_draft(MESSAGE, DraftBody(body=REPLY))

    assert refused.value.status_code == 403
    assert sent == []


async def test_a_failed_send_leaves_the_draft_to_try_again(drafted, sent, monkeypatch):
    async def failing_send(session, raw, thread_id=None):
        raise GoogleApiError(500, "backend error")

    monkeypatch.setattr(mail.gmail, "send", failing_send)

    with pytest.raises(HTTPException) as failed:
        await send_draft(MESSAGE, DraftBody(body=REPLY))

    assert failed.value.status_code == 502
    assert store.get(MESSAGE)["status"] == "suggested"


async def test_nothing_is_sent_into_a_thread_the_user_already_answered(
    drafted, sent, monkeypatch
):
    async def answered(session, message_id):
        return True

    monkeypatch.setattr(mail.gmail, "answered", answered)

    with pytest.raises(HTTPException) as refused:
        await send_draft(MESSAGE, DraftBody(body=REPLY))

    assert refused.value.status_code == 409
    assert sent == []
    assert store.get(MESSAGE)["status"] == "discarded"


async def test_a_discarded_draft_cannot_be_sent(drafted, sent):
    discard_draft(MESSAGE)

    with pytest.raises(HTTPException) as refused:
        await send_draft(MESSAGE, DraftBody(body=REPLY))

    assert refused.value.status_code == 409
    assert sent == []


async def test_an_email_with_no_draft_cannot_be_sent(sent):
    store.put(MESSAGE, Triage(gist="A notice.", needs_reply=False))

    with pytest.raises(HTTPException) as refused:
        await send_draft(MESSAGE, DraftBody(body="anything"))

    assert refused.value.status_code == 404
    assert sent == []


def test_changes_are_refused_without_the_apps_header():
    with pytest.raises(HTTPException) as refused:
        require_app(None)

    assert refused.value.status_code == 403
    require_app("1")


def _message(message_id: str, sender: str, subject: str, minutes_ago: int) -> dict:
    """A Gmail message as its search returns it: headers only."""
    return {
        "id": message_id,
        "threadId": f"thread-{message_id}",
        "internalDate": str(1_790_000_000_000 - minutes_ago * 60_000),
        "snippet": "",
        "labelIds": ["INBOX"],
        "payload": {
            "headers": [
                {"name": "From", "value": sender},
                {"name": "Subject", "value": subject},
            ]
        },
    }


@pytest.fixture
def inbox(monkeypatch) -> dict[str, list[dict]]:
    """Gmail's two searches answer from here, and nothing is read by the model."""
    found: dict[str, list[dict]] = {mail.UNREAD_QUERY: [], mail.RECENT_QUERY: []}

    async def search_ids(session, query="", limit=5):
        return [message["id"] for message in found[query]]

    async def raw_messages(session, message_ids):
        known = {m["id"]: m for messages in found.values() for m in messages}
        return [known[message_id] for message_id in message_ids]

    monkeypatch.setattr(mail.gmail, "search_ids", search_ids)
    monkeypatch.setattr(mail.gmail, "raw_messages", raw_messages)
    monkeypatch.setattr(service, "start_reading", lambda ids: None)
    return found


async def test_recent_mail_is_listed_only_while_a_reply_is_owed(inbox):
    inbox[mail.RECENT_QUERY] = [
        _message("asks", "Asha Verma <asha@example.com>", "Chapter 3", 10),
        _message("notice", "Course Office <office@example.com>", "Timetable", 20),
        _message("unseen", "Rahul Nair <rahul@example.com>", "Lab slots", 30),
    ]
    store.put(
        "asks", Triage(gist="Asha asks about Friday.", needs_reply=True, reply=REPLY)
    )
    store.put("notice", Triage(gist="The timetable is out.", needs_reply=False))

    items = (await waiting_mail(None)).items

    assert [item.id for item in items] == ["asks"]
    assert items[0].draft.status == "suggested"

    discard_draft("asks")
    items = (await waiting_mail(None)).items

    assert items == []


async def test_unread_important_mail_is_listed_whatever_orbit_makes_of_it(inbox):
    inbox[mail.UNREAD_QUERY] = [
        _message("old", "Course Office <office@example.com>", "Fees", 50_000),
    ]
    inbox[mail.RECENT_QUERY] = [
        _message("asks", "Asha Verma <asha@example.com>", "Chapter 3", 10),
    ]
    store.put("old", Triage(gist="Fees are due.", needs_reply=False))
    store.put(
        "asks", Triage(gist="Asha asks about Friday.", needs_reply=True, reply=REPLY)
    )

    items = (await waiting_mail(None)).items

    assert [item.id for item in items] == ["asks", "old"]


async def test_an_email_found_by_both_searches_is_listed_once(inbox):
    both = _message("asks", "Asha Verma <asha@example.com>", "Chapter 3", 10)
    inbox[mail.UNREAD_QUERY] = [both]
    inbox[mail.RECENT_QUERY] = [both]

    items = (await waiting_mail(None)).items

    assert [item.id for item in items] == ["asks"]


@pytest.fixture
def reader(monkeypatch) -> dict:
    """Gmail and the model are replaced: the tests say what each returns."""
    world: dict = {"answered": set(), "unfetchable": set(), "asked": [], "model": None}

    async def read(session, message_id):
        if message_id in world["unfetchable"]:
            raise GoogleApiError(404, "not found")
        return {"id": message_id, "from": "asha@example.com", "subject": "Chapter 3"}

    async def answered(session, message_id):
        return message_id in world["answered"]

    async def names(session):
        return ("Nidhish Kumar", "Nidhish")

    async def triage(emails, *, name, given_name):
        world["asked"].append([email["id"] for email in emails])
        return await world["model"](emails)

    async def drafts_for_all(emails):
        return [
            Triage(gist="Asha asks about Friday.", needs_reply=True, reply=REPLY)
            for _ in emails
        ]

    world["model"] = drafts_for_all
    monkeypatch.setattr(service.gmail, "read", read)
    monkeypatch.setattr(service.gmail, "answered", answered)
    monkeypatch.setattr(service, "_user_names", names)
    monkeypatch.setattr(service, "triage", triage)
    return world


async def test_each_email_is_read_in_a_request_of_its_own(reader):
    await service._read_all(["a", "b", "c"])

    assert sorted(reader["asked"]) == [["a"], ["b"], ["c"]]
    assert all(store.get(m)["status"] == "suggested" for m in ("a", "b", "c"))


async def test_no_reply_is_drafted_for_a_thread_the_user_already_answered(reader):
    reader["answered"] = {"b"}

    await service._read_all(["a", "b"])

    assert store.get("a")["reply"] == REPLY
    record = store.get("b")
    assert record["gist"] == "Asha asks about Friday."
    assert record["reply"] is None
    assert record["status"] == "none"


async def test_an_email_that_cannot_be_fetched_does_not_hold_up_the_rest(reader):
    reader["unfetchable"] = {"b"}

    await service._read_all(["a", "b", "c"])

    assert sorted(reader["asked"]) == [["a"], ["c"]]
    assert store.get("a") and store.get("c")
    assert store.get("b") is None
    assert service.trouble(["a", "b", "c"]) == service.COULD_NOT_READ


async def test_an_email_the_model_skipped_is_left_for_later(reader):
    async def skips_b(emails):
        return [
            None if email["id"] == "b" else Triage(gist="A notice.", needs_reply=False)
            for email in emails
        ]

    reader["model"] = skips_b

    await service._read_all(["a", "b"])

    assert store.get("a")["gist"] == "A notice."
    assert store.get("b") is None


async def test_a_failed_read_is_reported_and_not_retried_straight_away(reader):
    async def refuses(emails):
        raise ModelError("Ollama is not signed in. Run `ollama signin`.")

    reader["model"] = refuses

    await service._read_all(["a"])
    service.start_reading(["a"])

    assert reader["asked"] == [["a"]]
    assert not service.still_reading(["a"])
    assert "Ollama is not signed in" in service.trouble(["a"])


async def test_each_failure_waits_longer_before_the_next_try(reader, monkeypatch):
    async def refuses(emails):
        raise ModelError("the Ollama usage limit has been reached.")

    reader["model"] = refuses
    clock = [1000.0]
    monkeypatch.setattr(service.time, "monotonic", lambda: clock[0])

    await service._read_all(["a"])
    first_wait = service._failed["a"][0] - clock[0]
    await service._read_all(["a"])
    second_wait = service._failed["a"][0] - clock[0]

    assert first_wait == service.RETRY_SECONDS
    assert second_wait == 4 * service.RETRY_SECONDS


async def test_the_reason_mail_was_not_read_is_reported(inbox, reader):
    async def refuses(emails):
        raise ModelError("Ollama on this computer could not be reached. Is it running?")

    reader["model"] = refuses
    inbox[mail.UNREAD_QUERY] = [
        _message("old", "Course Office <office@example.com>", "Fees", 50_000),
    ]
    await service._read_all(["old"])

    found = await waiting_mail(None)

    assert [item.id for item in found.items] == ["old"]
    assert found.items[0].gist is None
    assert "could not be reached" in found.trouble


def test_the_models_answer_is_matched_to_the_emails_by_number():
    answer = """```json
    {"emails": [
      {"number": 2, "gist": "Asha asks about Friday.", "needs_reply": true, "reply": "Hi Asha"},
      {"number": 1, "gist": "The timetable is out.", "needs_reply": false, "reply": "ignored"}
    ]}
    ```"""

    first, second, third = _in_order(answer, 3)

    assert (first.gist, first.needs_reply, first.reply) == (
        "The timetable is out.",
        False,
        None,
    )
    assert (second.needs_reply, second.reply) == (True, "Hi Asha")
    assert third is None


def test_an_answer_that_is_not_the_agreed_json_is_refused():
    with pytest.raises(ModelError):
        _in_order("Sure! Here is what I found in your inbox.", 2)


async def test_an_emails_headers_are_fetched_from_gmail_only_once(inbox, monkeypatch):
    fetched: list[list[str]] = []
    inbox[mail.UNREAD_QUERY] = [
        _message("old", "Course Office <office@example.com>", "Fees", 50_000),
    ]
    known = {"old": inbox[mail.UNREAD_QUERY][0]}

    async def raw_messages(session, message_ids):
        fetched.append(list(message_ids))
        return [known[message_id] for message_id in message_ids]

    monkeypatch.setattr(mail.gmail, "raw_messages", raw_messages)

    await waiting_mail(None)
    found = await waiting_mail(None)

    assert [item.id for item in found.items] == ["old"]
    assert [ids for ids in fetched if ids] == [["old"]]
