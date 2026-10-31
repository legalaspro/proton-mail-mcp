"""Later timer chunks must refresh protected membership and raw identity checks."""

from copy import deepcopy

import pytest
from test_folder_rules import rule
from test_rules_timer import arrive, baseline, changed, mark
from test_rules_timer import timer_case as timer_case

from proton_mail_mcp.mail import MailValidationError


@pytest.mark.parametrize("change", ["protected-source", "protected-membership", "ambiguous-id"])
def test_later_chunk_revalidates_protection_and_identity(timer_case, monkeypatch, change):
    case = timer_case(
        count=0, rules=[rule("subject", "First"), rule("subject", "Second", "Folders/Other")]
    )
    baseline(case)
    arrive(case, 1, subject="First")
    arrive(case, 2, subject="Second")
    apply = case.cleanup.backend.apply

    def apply_then_change(*args, **kwargs):
        outcome = apply(*args, **kwargs)
        if args[3]["folder"] == "Folders/Review":
            if change == "protected-source":
                item = case.fake.folders["INBOX"]["2"]
            else:
                item = deepcopy(case.fake.folders["All Mail"]["10002"])
                case.fake.folders["All Mail"]["10002"] = item
            if change == "ambiguous-id":
                item.headers = item.headers[:-2] + b"Message-ID: <colliding@example.test>\r\n\r\n"
            else:
                item.headers = item.headers.replace(b"sender@example.com", b"relative@gmail.com")
        return outcome

    monkeypatch.setattr(case.cleanup.backend, "apply", apply_then_change)
    if change == "ambiguous-id":
        with pytest.raises(MailValidationError):
            case.timer.run()
        assert mark(case)["uid"] == 1
    else:
        result = case.timer.run()
        assert result["count"] == 1 and result["skipped"] == 1
        assert mark(case)["uid"] == 2 and case.timer.run()["count"] == 0
    assert set(case.fake.folders["INBOX"]) == {"2"}
    assert not case.fake.folders["Folders/Other"]
    assert [call[1] for call in changed(case)] == ["1"]
