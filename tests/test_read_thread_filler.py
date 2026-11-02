"""Invisible preheader filler collapses, so read_thread's text holds the message itself."""

import pytest
from test_mail import HTML, PLAIN, client, message
from test_mail_safety import ReadOnlyImap

from proton_mail_mcp.config import Settings
from proton_mail_mcp.mail import _collapse_filler
from proton_mail_mcp.threading import MAX_THREAD_BODY_BYTES
from proton_mail_mcp.views import html_text

FILLER = " \u034f\u00a0\u200c" * 3000
BODY = "Dear reader, the spring catalogue is attached."


@pytest.fixture
def settings():
    return Settings(
        token="a-test-token-that-is-at-least-32-characters", bridge_user="reader@example.test"
    )


def read(settings, structure, payload):
    identifier = "<offer@example.test>"
    sized = structure.replace(b" 24 2 ", b" %d 2 " % len(payload)).replace(
        b" 90 1 ", b" %d 1 " % len(payload)
    )
    found = message(identifier, structure=sized, sections={"1": payload})
    fake = ReadOnlyImap({"INBOX": {"7": found}, "All Mail": {"501": found}}, uidnext=True)
    [item] = client(settings, fake).read_thread(identifier, include_thread=False)["messages"]
    return item


def test_a_preheader_of_plain_text_filler_becomes_one_space(settings):
    payload = ("Spring sale" + FILLER + "\n\n" + BODY).encode()
    assert len(payload) < MAX_THREAD_BODY_BYTES

    item = read(settings, PLAIN, payload)

    assert item["text"] == "Spring sale \n\n" + BODY
    assert item["truncated"] is False


def test_html_entity_filler_collapses_after_the_markup_is_stripped(settings):
    payload = ("<p>Spring sale" + "&#847;&zwnj;&nbsp;" * 2000 + "</p><p>" + BODY + "</p>").encode()

    item = read(settings, HTML, payload)

    assert item["text"] == "Spring sale \n\n" + BODY


@pytest.mark.parametrize(
    "text",
    [
        # Emoji sequences hold one joiner between pictures.
        "Family: \U0001f468\u200d\U0001f469\u200d\U0001f467 \U0001f3f3\ufe0f\u200d\U0001f308",
        # Persian writes one zero-width non-joiner inside a word.
        "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645 and a soft hy\u00adphen",
        "Plain text   with  spaces\tand\u00a0no filler",
    ],
)
def test_a_single_invisible_character_stays(text):
    assert _collapse_filler(text) == text


def test_the_thread_view_text_collapses_the_same_way():
    markup = "<p>Hi" + "&#847;&zwnj;&nbsp;" * 50 + "there</p>"
    assert html_text(markup) == "Hi there"
