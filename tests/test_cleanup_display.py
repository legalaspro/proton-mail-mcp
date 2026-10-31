"""Display decoding is deferred without changing samples, filters or undo digests."""

from copy import deepcopy

from test_cleanup_envelope import envelope

from proton_mail_mcp import cleanup_lookup
from proton_mail_mcp.action_log import header_digest
from proton_mail_mcp.cleanup_lookup import display_row, display_value, envelope_row
from proton_mail_mcp.guarded import CleanupService


def test_inventory_defers_display_parser_until_sample_or_digest(monkeypatch):
    item = envelope(sender=[[None, None, b"offers", b"updates.example.invalid"]])
    item["envelope"][1] = b"=?utf-8?Q?Weekly_=E2=98=83?="
    item["envelope"][0] = b"Fri, 9 Oct 2026 09:00:00 +0000"
    calls = []
    parser = cleanup_lookup.HeaderParser

    def counting_parser(*args, **kwargs):
        calls.append(True)
        return parser(*args, **kwargs)

    monkeypatch.setattr(cleanup_lookup, "HeaderParser", counting_parser)
    row = envelope_row(item, "7", "INBOX", "1")
    assert not calls
    before = deepcopy(row)
    assert display_value(row, "subject") == "Weekly \N{SNOWMAN}"
    sample = CleanupService._sample(row, {"INBOX"})
    assert sample["subject"] == "Weekly \N{SNOWMAN}"
    assert sample["date"] == "Fri, 09 Oct 2026 09:00:00 +0000"
    displayed = display_row(row)
    displayed.pop("_envelope_display")
    assert header_digest(row) == header_digest(displayed)
    assert row == before
