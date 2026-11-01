"""No arbitrary local paths: the allowed folder holds, against traversal and links."""

import os
import sys
from dataclasses import replace

import pytest
from attachment_fixtures import FakeImap, client, mail_with, part
from attachment_fixtures import settings as settings  # noqa: F401

from proton_mail_mcp import attachments as attachments_module
from proton_mail_mcp.mail import MailValidationError

MESSAGE = "<files@example.com>"
TRAVERSALS = [
    "../outside.txt",
    "../../outside.txt",
    "a/../../outside.txt",
    "/etc/passwd",
    "//etc/passwd",
    "..",
    ".",
    "",
    "a//b.txt",
    "a/./b.txt",
    "a\\..\\b.txt",
    "..\\outside.txt",
    "C:\\Windows\\win.ini",
    ".hidden",
    "a/.hidden",
    "name\x00.txt",
    "name\n.txt",
    "name\u202e.txt",
    "x" * 2000,
]
TOO_LONG_FOR_ONE_NAME = "x" * 201


@pytest.fixture
def layout(tmp_path):
    """The allowed folder, with secret files beside and above it."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"secret outside the folder")
    folder = tmp_path / "allowed"
    folder.mkdir()
    return folder, outside


def saver(settings, folder, name="plain.txt", data=b"attachment bytes"):
    configured = replace(settings, attachments_dir=str(folder))
    fake = FakeImap({"INBOX": {"7": mail_with(MESSAGE, part(name, "text/plain", data))}})
    return client(configured, fake), fake


def tree(path):
    return sorted(str(entry.relative_to(path)) for entry in path.rglob("*"))


@pytest.mark.parametrize("name", [*TRAVERSALS, "sub/file.txt", TOO_LONG_FOR_ONE_NAME])
def test_a_chosen_save_name_with_any_path_refuses_before_reading_mail(
    settings, layout, tmp_path, name
):
    folder, _ = layout
    before = tree(tmp_path)
    mail, fake = saver(settings, folder)
    if name == "":  # The empty default means the sender's cleaned name.
        return
    with pytest.raises(MailValidationError, match="plain relative paths"):
        mail.save_attachment(MESSAGE, 1, name)
    assert tree(tmp_path) == before
    assert fake.calls == []


@pytest.mark.parametrize(
    "hostile,expected",
    [
        ("../../outside.txt", "outside.txt"),
        ("..\\..\\outside.txt", "outside.txt"),
        ("/etc/cron.d/job", "job"),
        ("C:\\Users\\me\\report.pdf", "report.pdf"),
        (".bashrc", "bashrc"),
        ("...", "attachment-1.txt"),
        ("..", "attachment-1.txt"),
        ("-rf .pdf", "rf .pdf"),
        ("report.pdf   ", "report.pdf"),
        ("a\tb.pdf", "a_b.pdf"),
        ("invoice\u202efdp.exe", "invoice_fdp.exe"),
    ],
)
def test_a_senders_name_can_never_leave_the_folder(
    settings, layout, monkeypatch, hostile, expected
):
    folder, outside = layout
    mail, _ = saver(settings, folder, name="placeholder")
    # BODYSTRUCTURE strings can carry any characters; set the parsed name directly.
    named = {"filename": hostile, "type": "text/plain"}
    monkeypatch.setattr(attachments_module, "_fetch_parts", lambda *a, **k: [(1, named, b"data")])
    result = mail.save_attachment(MESSAGE, 1)
    assert result["filename"] == expected
    assert (folder / expected).read_bytes() == b"data"
    assert os.path.dirname(result["filename"]) == ""
    assert (outside / "secret.txt").read_bytes() == b"secret outside the folder"
    assert set(tree(folder)) == {expected}


def test_a_long_name_keeps_its_extension_and_fits_the_filesystem(settings, layout):
    folder, _ = layout
    name = attachments_module.safe_filename("ü" * 400 + ".pdf", "fallback")
    assert name.endswith(".pdf") and len(name.encode()) <= attachments_module.MAX_NAME_BYTES
    (folder / name).write_bytes(b"fits")


def test_saving_never_follows_a_link_at_the_target_name(settings, layout):
    folder, outside = layout
    (folder / "plain.txt").symlink_to(outside / "secret.txt")
    (folder / "dangling.txt").symlink_to(outside / "created-through-link.txt")
    mail, _ = saver(settings, folder)
    assert mail.save_attachment(MESSAGE, 1)["filename"] == "plain (1).txt"
    assert mail.save_attachment(MESSAGE, 1, "dangling.txt")["filename"] == "dangling (1).txt"
    assert (outside / "secret.txt").read_bytes() == b"secret outside the folder"
    assert not (outside / "created-through-link.txt").exists()
    assert (folder / "plain.txt").is_symlink()


def test_a_folder_that_is_a_link_refuses(settings, layout, tmp_path):
    _, outside = layout
    link = tmp_path / "link-to-outside"
    link.symlink_to(outside)
    mail, fake = saver(settings, link)
    with pytest.raises(MailValidationError, match="missing or a symbolic link"):
        mail.save_attachment(MESSAGE, 1)
    assert fake.calls == [] and sorted(os.listdir(outside)) == ["secret.txt"]


def test_a_missing_folder_or_none_at_all_refuses_before_reading_mail(settings, layout, tmp_path):
    mail, fake = saver(settings, tmp_path / "absent")
    with pytest.raises(MailValidationError, match="missing or a symbolic link"):
        mail.save_attachment(MESSAGE, 1)
    assert fake.calls == []
    fake = FakeImap({"INBOX": {"7": mail_with(MESSAGE, part("a.txt", "text/plain", b"x"))}})
    with pytest.raises(MailValidationError, match="set PROTON_MCP_ATTACHMENTS_DIR"):
        client(settings, fake).save_attachment(MESSAGE, 1)
    assert fake.calls == []


def test_a_folder_everyone_can_write_to_refuses(settings, layout):
    folder, _ = layout
    folder.chmod(0o777)
    mail, fake = saver(settings, folder)
    with pytest.raises(MailValidationError, match="writable by everyone"):
        mail.save_attachment(MESSAGE, 1)
    assert fake.calls == [] and list(folder.iterdir()) == []


def test_a_failed_write_leaves_no_file_and_reports_no_path(settings, layout, monkeypatch):
    folder, _ = layout
    real = os.fdopen

    def failing(descriptor, mode):
        real(descriptor, mode).close()
        raise OSError(f"disk full at {folder}")

    mail, _ = saver(settings, folder)
    monkeypatch.setattr(attachments_module.os, "fdopen", failing)
    with pytest.raises(MailValidationError, match="could not be written") as failed:
        mail.save_attachment(MESSAGE, 1)
    assert str(folder) not in str(failed.value)
    assert list(folder.iterdir()) == []


def drafter(settings, folder):
    configured = replace(settings, attachments_dir=str(folder))
    fake = FakeImap({"INBOX": {}, "Drafts": {}})
    return client(configured, fake), fake


def attach(mail, *names):
    return mail.create_draft(
        "to@example.com", subject="Files", body="See attached", attachments=list(names)
    )


@pytest.mark.parametrize("name", [value for value in TRAVERSALS if value])
def test_attaching_any_path_outside_the_folder_refuses_before_any_mail_work(settings, layout, name):
    folder, _ = layout
    mail, fake = drafter(settings, folder)
    with pytest.raises(MailValidationError, match="plain relative paths"):
        attach(mail, name)
    assert fake.calls == [] and fake.appended == []


def test_a_real_file_attaches_and_a_subfolder_is_inside_the_folder(settings, layout):
    folder, _ = layout
    (folder / "sub").mkdir()
    (folder / "plain.txt").write_bytes(b"plain")
    (folder / "sub" / "deep.txt").write_bytes(b"deep")
    mail, fake = drafter(settings, folder)
    attach(mail, "plain.txt", "sub/deep.txt")
    assert len(fake.appended) == 1


def test_a_link_to_a_file_outside_refuses(settings, layout):
    folder, outside = layout
    (folder / "innocent.txt").symlink_to(outside / "secret.txt")
    mail, fake = drafter(settings, folder)
    with pytest.raises(MailValidationError, match="No readable regular file"):
        attach(mail, "innocent.txt")
    assert fake.appended == []


def test_a_link_to_a_directory_outside_refuses(settings, layout):
    folder, outside = layout
    (folder / "shared").symlink_to(outside)
    mail, fake = drafter(settings, folder)
    with pytest.raises(MailValidationError, match="No readable regular file"):
        attach(mail, "shared/secret.txt")
    assert fake.appended == []


def test_a_link_that_stays_inside_the_folder_refuses_too(settings, layout):
    folder, _ = layout
    (folder / "real.txt").write_bytes(b"real")
    (folder / "alias.txt").symlink_to(folder / "real.txt")
    mail, _ = drafter(settings, folder)
    with pytest.raises(MailValidationError, match="No readable regular file"):
        attach(mail, "alias.txt")


def test_a_link_that_replaces_a_checked_file_is_not_followed(settings, layout, monkeypatch):
    folder, outside = layout
    target = folder / "report.txt"
    target.write_bytes(b"checked")
    mail, _ = drafter(settings, folder)
    # The file is swapped for a link between any earlier look at it and the read.
    assert attachments_module.local_attachments(mail, ["report.txt"])[0]["size"] == 7
    target.unlink()
    target.symlink_to(outside / "secret.txt")
    with pytest.raises(MailValidationError, match="No readable regular file"):
        attachments_module._read_local(mail.settings, "report.txt")


def test_a_hard_link_refuses(settings, layout):
    folder, outside = layout
    os.link(outside / "secret.txt", folder / "linked.txt")
    mail, fake = drafter(settings, folder)
    with pytest.raises(MailValidationError, match="No readable regular file"):
        attach(mail, "linked.txt")
    assert fake.appended == []


def test_a_directory_or_a_fifo_refuses_and_a_fifo_does_not_hang(settings, layout):
    folder, _ = layout
    (folder / "directory").mkdir()
    os.mkfifo(folder / "pipe")
    mail, _ = drafter(settings, folder)
    for name in ("directory", "pipe", "missing.txt"):
        with pytest.raises(MailValidationError, match="No readable regular file"):
            attach(mail, name)


def test_the_upload_cap_refuses_without_reading_a_huge_file(settings, layout, monkeypatch):
    folder, _ = layout
    with open(folder / "sparse.bin", "wb") as stream:
        # NTFS allocates a file's whole size where a POSIX filesystem leaves it sparse.
        stream.truncate((64 if sys.platform == "win32" else 50 * 1024) * 1024 * 1024)
    mail, fake = drafter(settings, folder)
    with pytest.raises(MailValidationError, match="7 MiB limit"):
        attach(mail, "sparse.bin")
    with pytest.raises(MailValidationError, match="7 MiB limit"):
        attachments_module.local_attachments(mail, ["sparse.bin"])
    assert fake.appended == []


def test_a_file_growing_past_the_cap_after_its_size_was_checked_refuses(
    settings, layout, monkeypatch
):
    folder, _ = layout
    monkeypatch.setattr(attachments_module, "MAX_UPLOAD_BYTES", 8)
    (folder / "grows.txt").write_bytes(b"x" * 9)
    real = os.fstat
    small = os.stat_result((0o100600, 0, 0, 1, 0, 0, 4, 0, 0, 0))
    monkeypatch.setattr(attachments_module.os, "fstat", lambda fd: small if fd > 2 else real(fd))
    mail, fake = drafter(settings, folder)
    with pytest.raises(MailValidationError, match="limit of one draft"):
        attach(mail, "grows.txt")
    assert fake.appended == []


def test_files_cannot_attach_without_the_folder_setting(settings):
    fake = FakeImap({"Drafts": {}})
    with pytest.raises(MailValidationError, match="set PROTON_MCP_ATTACHMENTS_DIR"):
        attach(client(settings, fake), "plain.txt")
    assert fake.calls == []
