"""PROTON_MCP_SEND_TYPED_OK_CLIENTS and the confirmation logs: synthetic drafts, real sessions."""

import asyncio
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.exceptions import McpError
from mcp.types import METHOD_NOT_FOUND, ErrorData
from test_send import (
    BODY,
    DRAFT_ID,
    RECIPIENT,
    SENDER,
    SUBJECT,
    Host,
    local_certificate,  # noqa: F401
    records,
)
from test_send import rig as _send_rig
from test_send_http import _HTTP
from test_send_token_approval import own_rig as _own_rig
from test_send_token_config import BASE
from test_send_typed_ok import call
from test_unsubscribe import IDENTIFIER
from test_unsubscribe_server import setup_tools

from proton_mail_mcp import confirmation
from proton_mail_mcp import send as send_module
from proton_mail_mcp.config import ConfigError, Settings
from proton_mail_mcp.confirmation import (
    ConfirmationError,
    declares_no_form,
    form_available,
    log_list_use,
    log_session_client,
    require_confirmation,
)
from proton_mail_mcp.server import create_app

rig = _send_rig
own_rig = _own_rig
# conftest forbids subprocess.run during tests; this one runs local Python only.
REAL_RUN = subprocess.run
ENV = "PROTON_MCP_SEND_TYPED_OK_CLIENTS"
FORM = {"elicitation": {"form": {}}}


def parsed(value):
    return Settings.from_env({**BASE, ENV: value}).send_typed_ok_clients


def listing(rig, value="Example Client", **changes):
    rig.settings = replace(rig.settings, send_typed_ok_clients=parsed(value), **changes)
    return rig


def host(name="Example Client", *, support=True, action="accept"):
    made = Host(action, support=support)
    made.session.client_params.clientInfo = SimpleNamespace(name=name, version="1.2")
    return made


async def session(rig, app, capabilities=FORM, name="Example Client", version="1.2"):
    http = _HTTP(app, rig.settings.token)
    await http.rpc(
        "initialize",
        {
            "protocolVersion": "2025-11-25",
            "capabilities": capabilities,
            "clientInfo": {"name": name, "version": version},
        },
    )
    await http.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
    http.on_elicit = lambda prompt: {"action": "accept", "content": {"confirm": True}}
    return http


def serve(rig, script):
    async def run():
        app = create_app(rig.settings, rig.client)
        async with app.app.router.lifespan_context(app.app):
            return await script(app)

    return asyncio.run(run())


def preview_of(result):
    return result["structuredContent"]


def lines(caplog):
    # Never caplog.at_level: the root stays at WARNING, as on the service.
    return [
        record.getMessage() for record in caplog.records if record.name == confirmation.__name__
    ]


def test_a_listed_client_declaring_a_form_gets_typed_ok_and_is_never_asked(rig):
    listing(rig)

    async def script(app):
        http = await session(rig, app)
        preview = preview_of(await call(http, "preview_send", draft_id=DRAFT_ID))
        assert preview["approval"] == "typed_ok"
        assert preview["next_steps"].startswith("This app has no confirmation form")
        assert RECIPIENT in preview["summary"]
        review = await call(http, "review_send", draft_id=DRAFT_ID)
        assert json.loads(review["content"][0]["text"])["approval"] == "typed_ok"
        sent = await call(http, "send_draft", token=preview["token"], draft_id=DRAFT_ID)
        assert sent["structuredContent"]["sent"] is True
        assert http.elicitations == []

    serve(rig, script)
    assert len(rig.smtp.sent) == 1
    assert [row["approval"] for row in records(rig) if row.get("budget_reserved")] == ["typed_ok"]


@pytest.mark.parametrize("name", ["Example", "Example Client 2", "Other"])
def test_an_unlisted_client_with_a_form_keeps_the_form(rig, name):
    listing(rig)

    async def script(app):
        http = await session(rig, app, name=name)
        preview = preview_of(await call(http, "preview_send", draft_id=DRAFT_ID))
        assert preview["approval"] == "host_confirmation"
        assert "summary" not in preview
        await call(http, "send_draft", token=preview["token"])
        assert len(http.elicitations) == 1

    serve(rig, script)
    assert len(rig.smtp.sent) == 1


def test_a_form_token_redeemed_in_a_listed_session_refuses_without_asking(rig, caplog):
    listing(rig)

    async def script(app):
        other = await session(rig, app, name="Other")
        preview = preview_of(await call(other, "preview_send", draft_id=DRAFT_ID))
        assert preview["approval"] == "host_confirmation"
        listed = await session(rig, app)
        refused = await call(listed, "send_draft", token=preview["token"], draft_id=DRAFT_ID)
        assert refused["isError"] is True
        assert listed.elicitations == [] and other.elicitations == []

    serve(rig, script)
    assert rig.smtp.calls == []
    assert "Confirmation not accepted: reason unsupported" in lines(caplog)


def test_a_listed_typed_ok_token_takes_the_form_in_an_unlisted_session(rig):
    listing(rig)

    async def script(app):
        listed = await session(rig, app)
        preview = preview_of(await call(listed, "preview_send", draft_id=DRAFT_ID))
        assert preview["approval"] == "typed_ok"
        other = await session(rig, app, name="Other")
        await call(other, "send_draft", token=preview["token"], draft_id=DRAFT_ID)
        assert len(other.elicitations) == 1

    serve(rig, script)
    assert [row["approval"] for row in records(rig) if row.get("budget_reserved")] == [
        "host_confirmation"
    ]


@pytest.mark.parametrize("value", [None, "", " ", ",", " , ,, "])
def test_an_empty_or_unset_list_changes_nothing(value):
    env = BASE if value is None else {**BASE, ENV: value}
    assert Settings.from_env(env).send_typed_ok_clients == ()
    assert Settings.from_env(env).no_form_clients == ()
    for name in ("Example Client", "", " "):
        client = host(name)
        assert form_available(client, ()) is True
        assert declares_no_form(client, ()) is False
    assert declares_no_form(host(support=False), ()) is True


@pytest.mark.parametrize(
    "value",
    [
        "Example Client",
        "example client",
        "EXAMPLE CLIENT",
        "  Example Client  ",
        "other, example client ,",
        ",,EXAMPLE client,other",
    ],
)
@pytest.mark.parametrize(
    "name", ["Example Client", "example client", "EXAMPLE CLIENT", "  Example Client\t"]
)
def test_a_name_matches_exactly_ignoring_case_and_surrounding_space(value, name):
    client = host(name)
    assert form_available(client, parsed(value)) is False
    assert declares_no_form(client, parsed(value)) is True


@pytest.mark.parametrize(
    "name", ["Example", "ExampleClient", "Example  Client", "Example Client,other", "", "  "]
)
def test_a_partial_or_empty_name_never_matches(name):
    client = host(name)
    assert form_available(client, parsed("Example Client, ,")) is True
    assert declares_no_form(client, parsed("Example Client, ,")) is False


@pytest.mark.parametrize("params", [None, SimpleNamespace(), SimpleNamespace(capabilities=None)])
def test_an_unreadable_client_never_opens_the_typed_ok(params):
    client = SimpleNamespace(session=SimpleNamespace(client_params=params))
    assert form_available(client, ("example client",)) is False
    assert declares_no_form(client, ("example client",)) is False


def test_the_list_must_be_a_tuple():
    with pytest.raises(ConfigError, match=ENV):
        replace(Settings.from_env(BASE), send_typed_ok_clients="example client")


def test_strict_overrides_the_list(rig):
    listing(rig, send_typed_ok=False)
    assert rig.settings.send_typed_ok_clients == ("example client",)
    assert rig.settings.no_form_clients == ()

    async def script(app):
        http = await session(rig, app)
        preview = preview_of(await call(http, "preview_send", draft_id=DRAFT_ID))
        assert preview["approval"] == "host_confirmation"
        assert "summary" not in preview
        await call(http, "send_draft", token=preview["token"])
        assert len(http.elicitations) == 1

    serve(rig, script)
    assert len(rig.smtp.sent) == 1


@pytest.mark.parametrize("decision", ["decline", "cancel"])
def test_a_real_form_stays_strict_with_a_list_set(rig, decision, caplog):
    listing(rig)

    async def script(app):
        http = await session(rig, app, name="Other")
        http.on_elicit = lambda prompt: {"action": decision}
        first = preview_of(await call(http, "preview_send", draft_id=DRAFT_ID))
        refused = await call(http, "send_draft", token=first["token"])
        assert refused["isError"] is True
        second = preview_of(await call(http, "preview_send", draft_id=DRAFT_ID))
        assert second["approval"] == "host_confirmation"

    serve(rig, script)
    assert rig.smtp.calls == []
    assert f"Confirmation not accepted: reason {decision}" in lines(caplog)


def test_a_listed_client_is_refused_a_mailbox_form_without_being_asked(caplog):
    client = host()
    with pytest.raises(ConfirmationError) as refused:
        asyncio.run(
            require_confirmation(client, "summary", timeout=5, no_form_clients=("example client",))
        )
    assert refused.value.result == "confirmation_unavailable"
    assert client.prompts == []
    assert lines(caplog) == ["Confirmation not accepted: reason unsupported"]


def test_a_listed_client_is_refused_a_mailbox_form_through_the_server(rig, monkeypatch):
    listing(rig)
    _, fake, tool = setup_tools(rig, monkeypatch)
    preview = tool("preview_action", {"action": "unsubscribe", "ids": [IDENTIFIER]}, host())
    client = host()
    with pytest.raises(ToolError, match="this client did not show a form"):
        tool("unsubscribe", {"token": preview["token"]}, client)
    assert client.prompts == []
    assert not fake.appended


def test_a_listed_clients_mailto_unsubscribe_preview_takes_the_typed_ok(rig, monkeypatch, caplog):
    listing(rig, confirm_mutations=False)
    _, _, tool = setup_tools(rig, monkeypatch)
    client = host()
    preview = tool("preview_action", {"action": "unsubscribe", "ids": [IDENTIFIER]}, client)
    result = tool("unsubscribe", {"token": preview["token"]}, client)
    assert result["drafts"][0]["send_preview"]["approval"] == "typed_ok"
    assert client.prompts == []
    assert [line for line in lines(caplog) if ENV in line] == [
        f'Client name "Example Client", version "1.2" is on {ENV}: typed OK instead of the form'
    ]


def test_a_timeout_is_refused_and_logged_with_no_fallback_to_the_typed_ok(caplog):
    class Silent(Host):
        async def elicit(self, *, message, schema):
            await asyncio.sleep(60)

    with pytest.raises(ConfirmationError) as refused:
        asyncio.run(require_confirmation(Silent(), "summary", timeout=0.01))
    assert refused.value.result == "confirmation_timeout"
    assert lines(caplog) == ["Confirmation not accepted: reason timeout"]


@pytest.mark.parametrize(
    "client,reason",
    [
        (Host("decline"), "decline"),
        (Host("cancel"), "cancel"),
        (Host(confirm=False), "decline"),
        (Host(support=False), "unsupported"),
        (Host(McpError(ErrorData(code=METHOD_NOT_FOUND, message="no"))), "unsupported"),
        (Host(RuntimeError("to someone@example.test")), "error"),
    ],
    ids=["decline", "cancel", "unconfirmed", "no-capability", "no-method", "failure"],
)
def test_every_refused_form_logs_only_its_reason(client, reason, caplog):
    with pytest.raises(ConfirmationError):
        asyncio.run(require_confirmation(client, "to someone@example.test", timeout=5))
    assert lines(caplog) == [f"Confirmation not accepted: reason {reason}"]


def test_a_session_logs_its_client_once_and_the_list_each_time(rig, caplog):
    listing(rig)

    async def script(app):
        http = await session(rig, app)
        preview = preview_of(await call(http, "preview_send", draft_id=DRAFT_ID))
        await call(http, "send_draft", token=preview["token"], draft_id=DRAFT_ID)
        await session(rig, app, FORM, name="Other", version="9")
        return preview["token"]

    token = serve(rig, script)
    named = 'Client name "Example Client", version "1.2" is on ' + ENV
    assert lines(caplog) == [
        'MCP client name "Example Client", version "1.2", elicitation declared: True',
        f"{named}: typed OK instead of the form",
        f"{named}: typed OK instead of the form",
    ]
    for private in (SENDER, RECIPIENT, SUBJECT, BODY, token, rig.settings.token):
        assert private not in caplog.text
    assert "@" not in caplog.text


def test_a_review_logs_the_list_only_for_a_listed_client(rig, caplog):
    listing(rig)

    async def script(app):
        await call(await session(rig, app), "review_send", draft_id=DRAFT_ID)
        await call(await session(rig, app, name="Other"), "review_send", draft_id=DRAFT_ID)

    serve(rig, script)
    assert [line for line in lines(caplog) if ENV in line] == [
        f'Client name "Example Client", version "1.2" is on {ENV}: typed OK instead of the form'
    ]


def test_an_own_address_preview_on_a_listed_client_never_claims_the_typed_ok(own_rig, caplog):
    listing(own_rig)

    async def script(app):
        http = await session(own_rig, app)
        preview = preview_of(await call(http, "preview_send", draft_id=DRAFT_ID))
        assert preview["approval"] == "token"
        review = await call(http, "review_send", draft_id=DRAFT_ID)
        assert json.loads(review["content"][0]["text"])["approval"] == "token"

    serve(own_rig, script)
    assert lines(caplog) == [
        'MCP client name "Example Client", version "1.2", elicitation declared: True'
    ]


def test_a_second_session_logs_for_itself_and_no_list_line_without_a_list(rig, caplog):
    async def script(app):
        for capabilities, name in ((FORM, "One"), ({}, "Two")):
            http = await session(rig, app, capabilities, name=name)
            await call(http, "preview_send", draft_id=DRAFT_ID)

    serve(rig, script)
    assert lines(caplog) == [
        'MCP client name "One", version "1.2", elicitation declared: True',
        'MCP client name "Two", version "1.2", elicitation declared: False',
    ]


def test_a_listed_client_with_no_form_anyway_owes_nothing_to_the_list(caplog):
    assert declares_no_form(host(support=False), ("example client",)) is True
    log_list_use(host(support=False), ("example client",))
    assert lines(caplog) == []


def test_asking_which_path_applies_logs_nothing(caplog):
    assert declares_no_form(host(), ("example client",)) is True
    assert lines(caplog) == []


def test_logging_the_list_never_fails_the_call(caplog):
    log_list_use(None, ("example client",))
    log_list_use(SimpleNamespace(session=SimpleNamespace(client_params=None)), ("example client",))
    log_list_use(host(), ())
    assert lines(caplog) == []


def test_a_client_name_cannot_forge_a_line_or_put_an_address_in_the_log(rig, caplog):
    async def script(app):
        http = await session(
            rig, app, name="tool\nForged: line someone@example.test", version="2 a@b.test"
        )
        await call(http, "preview_send", draft_id=DRAFT_ID)

    serve(rig, script)
    [line] = lines(caplog)
    assert line == (
        'MCP client name "tool\\nForged: line [address]", version "2 [address]", '
        "elicitation declared: True"
    )


@pytest.mark.parametrize(
    "name,shown",
    [
        ('"a b"@example.test', "[address]"),
        ('tool "a b"@example.test', "tool [address]"),
        ("x" * 150 + "@example.test", "[address]"),
        ("n" * 150, "n" * 100),
        ("n" * 99 + " someone@example.test", "n" * 99 + " "),
    ],
)
def test_an_address_is_masked_before_the_length_cut(rig, caplog, name, shown):
    async def script(app):
        http = await session(rig, app, name=name)
        await call(http, "preview_send", draft_id=DRAFT_ID)

    serve(rig, script)
    assert lines(caplog) == [
        f"MCP client name {json.dumps(shown)}, version " + '"1.2", elicitation declared: True'
    ]


def test_the_typed_ok_warning_masks_an_address_shaped_client(rig, caplog):
    async def script(app):
        http = await session(rig, app, {}, name="me@example.test", version="2 a@b.test")
        preview = preview_of(await call(http, "preview_send", draft_id=DRAFT_ID))
        await call(http, "send_draft", token=preview["token"], draft_id=DRAFT_ID)

    serve(rig, script)
    warned = [
        record.getMessage()
        for record in caplog.records
        if record.name == send_module.__name__ and "Typed OK send path" in record.getMessage()
    ]
    assert len(warned) == 2
    for line in warned:
        assert 'client name "[address]", version "2 [address]"' in line


def test_a_scoped_package_name_stays_readable(rig, caplog):
    async def script(app):
        http = await session(rig, app, name="@scope/client")
        await call(http, "preview_send", draft_id=DRAFT_ID)

    serve(rig, script)
    assert lines(caplog) == [
        'MCP client name "@scope/client", version "1.2", elicitation declared: True'
    ]


def test_an_uninitialized_session_logs_nothing_and_never_raises(caplog):
    log_session_client(None)
    log_session_client(SimpleNamespace(session=SimpleNamespace(client_params=None)))
    assert lines(caplog) == []


def test_the_lines_reach_stderr_behind_the_servers_warning_root(tmp_path):
    """create_mcp sets the root to WARNING, so this needs a process pytest has not configured."""
    code = """
import logging
import uvicorn
from proton_mail_mcp.config import Settings
from proton_mail_mcp.confirmation import ConfirmationError
from proton_mail_mcp.server import create_mcp

settings = Settings(
    token="t" * 32,
    bridge_user="user@example.test",
    action_log_path=%r,
    identity_index_enabled=False,
)
create_mcp(settings, object())
uvicorn.Config("unused:app", access_log=False, log_level="warning", ws="none")
assert logging.getLogger().level == logging.WARNING
logging.getLogger("proton_mail_mcp.send").info("dropped")
ConfirmationError("confirmation_timeout")
""" % str(tmp_path / "actions.jsonl")
    source = str(Path(__file__).resolve().parents[1] / "src")
    done = REAL_RUN(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": source},
    )
    assert done.returncode == 0, done.stderr
    assert "Confirmation not accepted: reason timeout" in done.stderr
    assert "dropped" not in done.stderr


def test_docs_say_it_is_off_self_reported_and_the_operators_choice():
    root = Path(__file__).resolve().parents[1]
    readme = " ".join((root / "README.md").read_text(encoding="utf-8").split())
    assert f"| `{ENV}` | empty | Off by default; an operator choice" in readme
    assert "self-reported and unauthenticated, the same trust level as the elicitation" in readme
    assert "a quoted name keeps its quotes and never matches" in readme
    changelog = " ".join((root / "CHANGELOG.md").read_text(encoding="utf-8").split())
    assert f"`{ENV}`, a comma list of `clientInfo.name` values, empty by default" in changelog
    assert "the name is self-reported, the same trust level as the elicitation flag" in changelog
