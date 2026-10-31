"""With no folder set, attachment tools use a private per-user one; the rules stay the same."""

import os
import stat

import pytest
from attachment_fixtures import FakeImap, client, mail_with, part

from proton_mail_mcp.attachments import MAX_SAVE_BYTES
from proton_mail_mcp.config import ConfigError, Settings, default_attachments_dir
from proton_mail_mcp.mail import MailValidationError

MESSAGE = "<files@example.com>"
BASE = {"PROTON_MCP_TOKEN": "t" * 43, "PROTON_BRIDGE_USER": "reader@example.com"}


def from_env(**values):
    return Settings.from_env({**BASE, **values})


def saver(settings, data=b"attachment bytes"):
    fake = FakeImap({"INBOX": {"7": mail_with(MESSAGE, part("plain.txt", "text/plain", data))}})
    return client(settings, fake), fake


@pytest.fixture
def default_folder(tmp_path):
    return tmp_path / "home" / ".local" / "share" / "proton-mail-mcp" / "attachments"


@pytest.fixture
def default_settings(tmp_path):
    return from_env(HOME=str(tmp_path / "home"))


@pytest.fixture
def usual_umask():
    previous = os.umask(0o022)
    yield
    os.umask(previous)


def test_unset_means_the_folder_under_the_home():
    settings = from_env(HOME="/home/reader")
    assert settings.attachments_dir == "/home/reader/.local/share/proton-mail-mcp/attachments"
    assert settings.attachments_dir_default


def test_an_empty_setting_counts_as_unset():
    settings = from_env(HOME="/home/reader", PROTON_MCP_ATTACHMENTS_DIR="")
    assert settings.attachments_dir == "/home/reader/.local/share/proton-mail-mcp/attachments"
    assert settings.attachments_dir_default


def test_the_home_comes_from_the_system_when_the_environment_has_none(monkeypatch):
    monkeypatch.setattr(os.path, "expanduser", lambda _path: "/home/account")
    assert from_env().attachments_dir == "/home/account/.local/share/proton-mail-mcp/attachments"


@pytest.mark.parametrize(
    "data,expected",
    [
        ("/data", "/data/proton-mail-mcp/attachments"),
        # Not normalized, which the setting itself would refuse: this one is no one's typing.
        ("/data/", "/data/proton-mail-mcp/attachments"),
        ("/a/../data", "/data/proton-mail-mcp/attachments"),
        # The XDG specification ignores relative values.
        ("relative", "/home/reader/.local/share/proton-mail-mcp/attachments"),
        ("~/data", "/home/reader/.local/share/proton-mail-mcp/attachments"),
        ("", "/home/reader/.local/share/proton-mail-mcp/attachments"),
    ],
)
def test_an_absolute_xdg_data_home_replaces_the_home(data, expected):
    assert from_env(HOME="/home/reader", XDG_DATA_HOME=data).attachments_dir == expected


def test_the_setting_overrides_the_default():
    settings = from_env(
        HOME="/home/reader", XDG_DATA_HOME="/data", PROTON_MCP_ATTACHMENTS_DIR="/srv/files"
    )
    assert settings.attachments_dir == "/srv/files"
    assert not settings.attachments_dir_default


def test_an_unusable_setting_still_stops_the_server_and_names_itself():
    with pytest.raises(ConfigError, match="PROTON_MCP_ATTACHMENTS_DIR"):
        from_env(HOME="/home/reader", PROTON_MCP_ATTACHMENTS_DIR="relative/dir")


@pytest.mark.parametrize(
    "env",
    [{"HOME": "relative"}, {"HOME": "/home/a\nb"}, {"XDG_DATA_HOME": "/data\x7f"}],
)
def test_no_usable_location_never_stops_the_server_and_the_tools_refuse(env):
    assert default_attachments_dir(env) is None
    settings = from_env(**env)
    assert settings.attachments_dir is None
    mail, fake = saver(settings)
    with pytest.raises(MailValidationError, match="set PROTON_MCP_ATTACHMENTS_DIR"):
        mail.save_attachment(MESSAGE, 1)
    assert fake.calls == []


@pytest.mark.usefixtures("usual_umask")
def test_the_first_save_creates_the_folder_private_with_its_parents(
    default_settings, default_folder
):
    mail, _fake = saver(default_settings)
    assert mail.save_attachment(MESSAGE, 1)["filename"] == "plain.txt"
    assert stat.S_IMODE(default_folder.stat().st_mode) == 0o700
    saved = default_folder / "plain.txt"
    assert saved.read_bytes() == b"attachment bytes"
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600


def test_a_later_save_never_overwrites(default_settings, default_folder):
    mail, _fake = saver(default_settings)
    mail.save_attachment(MESSAGE, 1)
    assert mail.save_attachment(MESSAGE, 1)["filename"] == "plain (1).txt"
    assert sorted(os.listdir(default_folder)) == ["plain (1).txt", "plain.txt"]


@pytest.mark.parametrize("mode", [0o700, 0o750, 0o755])
def test_an_existing_folder_keeps_its_mode(default_settings, default_folder, mode):
    default_folder.mkdir(parents=True)
    default_folder.chmod(mode)
    mail, _fake = saver(default_settings)
    mail.save_attachment(MESSAGE, 1)
    assert stat.S_IMODE(default_folder.stat().st_mode) == mode


def test_a_folder_set_by_the_setting_is_not_created(tmp_path):
    folder = tmp_path / "absent"
    mail, fake = saver(from_env(HOME=str(tmp_path), PROTON_MCP_ATTACHMENTS_DIR=str(folder)))
    with pytest.raises(MailValidationError, match="PROTON_MCP_ATTACHMENTS_DIR.*missing"):
        mail.save_attachment(MESSAGE, 1)
    assert fake.calls == [] and not folder.exists()


def test_only_a_save_creates_the_folder(default_settings, default_folder):
    fake = FakeImap({"Drafts": {}})
    with pytest.raises(MailValidationError, match="PROTON_MCP_ATTACHMENTS_DIR.*missing"):
        client(default_settings, fake).create_draft(
            ["reader@example.com"], subject="s", body="b", attachments=["plain.txt"]
        )
    assert not default_folder.parent.exists()


def test_a_link_at_the_default_folder_is_not_followed(default_settings, default_folder, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    default_folder.parent.mkdir(parents=True)
    default_folder.symlink_to(outside)
    mail, fake = saver(default_settings)
    with pytest.raises(MailValidationError, match="PROTON_MCP_ATTACHMENTS_DIR.*symbolic link"):
        mail.save_attachment(MESSAGE, 1)
    assert fake.calls == [] and os.listdir(outside) == []


def test_a_file_at_the_default_folder_refuses_and_names_the_setting(
    default_settings, default_folder
):
    default_folder.parent.mkdir(parents=True)
    default_folder.write_text("not a folder")
    mail, fake = saver(default_settings)
    with pytest.raises(MailValidationError, match="PROTON_MCP_ATTACHMENTS_DIR"):
        mail.save_attachment(MESSAGE, 1)
    assert fake.calls == []


def test_a_default_folder_that_cannot_be_created_says_so_and_reads_no_mail(
    default_settings, tmp_path
):
    (tmp_path / "home").write_text("not a folder")
    mail, fake = saver(default_settings)
    with pytest.raises(
        MailValidationError, match=r"\(PROTON_MCP_ATTACHMENTS_DIR\) could not be created"
    ):
        mail.save_attachment(MESSAGE, 1)
    assert fake.calls == []


def test_a_default_folder_everyone_can_write_to_refuses(default_settings, default_folder):
    default_folder.mkdir(parents=True)
    default_folder.chmod(0o777)
    mail, fake = saver(default_settings)
    with pytest.raises(MailValidationError, match="PROTON_MCP_ATTACHMENTS_DIR.*writable"):
        mail.save_attachment(MESSAGE, 1)
    assert fake.calls == []


@pytest.mark.parametrize("name", ["../outside.txt", "/etc/passwd", "a/b.txt", ".hidden"])
def test_the_default_folder_still_allows_no_path_out(default_settings, default_folder, name):
    mail, fake = saver(default_settings)
    with pytest.raises(MailValidationError, match="PROTON_MCP_ATTACHMENTS_DIR"):
        mail.save_attachment(MESSAGE, 1, filename=name)
    assert fake.calls == [] and not default_folder.exists()


def test_the_default_folder_keeps_the_save_cap(default_settings, default_folder):
    assert MAX_SAVE_BYTES == 25 * 1024 * 1024
    mail, _fake = saver(default_settings, b"x" * (MAX_SAVE_BYTES + 1))
    with pytest.raises(MailValidationError, match="25 MiB save limit"):
        mail.save_attachment(MESSAGE, 1)
    assert os.listdir(default_folder) == []
