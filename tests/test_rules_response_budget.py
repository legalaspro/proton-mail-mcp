"""Rule groups share one bounded response and continue their exact approved rows."""

from resume_assertions import assert_bound_progress, identity
from test_cleanup_mutations import uid_values
from test_folder_rules import mailbox as mailbox
from test_folder_rules import rule
from test_folder_rules_resume import slow_moves
from test_mail import message


def test_rule_groups_return_structured_bounded_partials_and_exact_remainders(mailbox, monkeypatch):
    case = mailbox(count=65, rules=[rule("subject", "First"), rule(folder="Folders/Other")])
    for uid, item in case.fake.folders["INBOX"].items():
        # 30 + 35 rows: a call can finish the first rule's group and still fit
        # the next group's first command allowance.
        if int(uid) <= 30:
            item.headers = item.headers.replace(b"Subject: Hello", b"Subject: First")
    clock = slow_moves(case, monkeypatch, timeout=120)
    preview = case.rules.preview_rules()
    assert preview["count"] == 65 and preview["token"]
    grant = case.cleanup.tokens.inspect(preview["token"], "apply_rules")
    approved = {identity(row) for row in grant.messages}
    by_uid = {row["uid"]: identity(row) for row in grant.messages}
    source = dict(case.fake.folders["INBOX"])
    original_uid = case.fake.uid
    actual = []

    def observe(command, *args):
        if command == "MOVE":
            bounds = [by_uid[uid] for uid in uid_values(args[0])]
            for bound in bounds:
                assert case.fake.current == bound[0]
                assert case.fake.folders[bound[0]][bound[2]] is source[bound[2]]
            result = original_uid(command, *args)
            actual.extend(bounds)
            return result
        return original_uid(command, *args)

    monkeypatch.setattr(case.fake, "uid", observe)
    arrival = message("<unapproved-budget-arrival@example.test>")
    token, results, durations = preview["token"], [], []
    while token:
        started = clock.elapsed
        result = case.rules.apply_rules(token)
        durations.append(clock.elapsed - started)
        assert durations[-1] < 50
        assert {
            "partial",
            "run_id",
            "done",
            "remaining",
            "uncertain",
            "stop_reason",
            "continuation_token",
        } <= result.keys()
        results.append(result)
        assert_bound_progress(case.cleanup, grant, results, actual)
        assert not result["uncertain"] and not result["blocked"]
        untouched = approved - set(actual)
        assert {uid: item for uid, item in case.fake.folders["INBOX"].items() if uid in source} == {
            row[2]: source[row[2]] for row in untouched
        }
        entries = [row for call in results for row in call["messages"]]
        for target in ("Folders/Review", "Folders/Other"):
            assert case.fake.folders[target] == {
                row["uid_after"]: source[row["uid_before"]]
                for row in entries
                if row["destination_folder"] == target
            }
        if len(results) == 1:
            assert result["partial"] and result["continuation_token"]
            case.fake.folders["INBOX"]["901"] = arrival
            case.fake.folders["All Mail"]["901"] = arrival
        if result["partial"]:
            assert result["stop_reason"]
        else:
            assert result["stop_reason"] is None
        token = result["continuation_token"]
        assert len(results) <= preview["estimated_calls"]
    assert len(results) > 1 and len(actual) == 65 and set(actual) == approved
    assert case.fake.folders["INBOX"] == {"901": arrival}
    assert len(case.fake.folders["Folders/Review"]) == 30
    assert len(case.fake.folders["Folders/Other"]) == 35
    # At least one response spans the rule boundary without renewing its clock.
    assert any(
        len({row["destination_folder"] for row in call["messages"]}) == 2 for call in results
    )
