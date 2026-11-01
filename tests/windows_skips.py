"""Which tests check facts only POSIX has, so a Windows run leaves them out, and why.

Each entry is a test file, a test in it, or one parametrized case. What these tests guard is
covered on Windows some other way where a reason says so; docs/windows.md lists the gaps. Tests
with a mark of their own, such as the installer's real-host flows, are not listed here.
"""

import functools
import os
import stat
import sys
import tempfile

_FIFO = "os.mkfifo does not exist on Windows, and no FIFO can appear at a file path"
_LINK = (
    "makes a symbolic link, which needs a privilege on Windows and does nothing under Wine; "
    "test_localfs.py and test_localfs_windows.py check the refusal of links"
)
_MODE = (
    "checks POSIX mode bits or owners; Windows has neither, and test_localfs.py and "
    "test_windows_acl.py check its access rules"
)
_PATHS = "names POSIX paths or defaults; test_windows_compat.py checks the Windows ones"
_OPEN = "replaces os.open or os.fstat, which the Windows side of localfs goes around or needs whole"
_HELD = (
    "moves a directory a save holds open and checks where the save lands; Windows refuses the "
    "move, as test_localfs.py checks"
)

_WAKE = (
    "needs shutdown from another thread to wake a blocked read, which it does not on Windows: "
    "the read ends at its socket timeout instead"
)
_DOTDOT = "Windows reads .. by name, before links, so this spelling names the same store"

_SKIPS = {
    "test_action_log_recovery.py": {
        "test_readonly_fifo_is_rejected_without_blocking": _FIFO,
    },
    "test_attachment_default_dir.py": _PATHS,
    "test_background_sync.py": {
        "test_foreground_wakes_a_really_blocked_read_without_waiting": _WAKE,
    },
    "test_metadata_unsolicited.py": {
        "test_expansion_socket_cut_uses_fresh_bounded_canonical_session": _WAKE,
    },
    "test_attachment_paths.py": {
        "test_saving_never_follows_a_link_at_the_target_name": _LINK,
        "test_a_folder_everyone_can_write_to_refuses": _MODE,
        "test_a_directory_or_a_fifo_refuses_and_a_fifo_does_not_hang": _FIFO,
        "test_a_file_growing_past_the_cap_after_its_size_was_checked_refuses": _OPEN,
    },
    "test_attachments.py": {
        "test_pdf_parsing_runs_apart_without_this_servers_environment": _PATHS,
        "test_the_allowed_folder_setting_is_read": _PATHS,
    },
    "test_cleanup_guard.py": {
        "test_cleanup_config_defaults_and_environment_paths": _PATHS,
    },
    "test_cleanup_mutations.py": {
        "test_action_log_is_append_only_private_and_never_stores_token": _OPEN,
        "test_action_log_refuses_symlink_and_busy_transaction": _LINK,
        "test_action_log_permission_is_restricted_without_rewriting_existing_content": _MODE,
        "test_action_log_open_denied_prevents_all_mutations": _OPEN,
    },
    "test_credential_store.py": {
        "test_settings_default_to_the_existing_keychain_and_reject_unknown_stores": _PATHS,
    },
    "test_identity_index.py": {
        "test_permissive_storage_never_reused": _MODE,
        "test_wrong_owner_storage_refuses": _MODE,
        "test_symlink_storage_refuses": _LINK,
        "test_fifo_inventory_refuses_without_blocking": _FIFO,
        "test_index_configuration_defaults_and_overrides": _PATHS,
    },
    "test_installer_cli.py": {
        "test_venv_option_reaches_install_and_uninstall_plans": _PATHS,
    },
    "test_installer_execute.py": {
        "test_download_refuses_plain_http_and_oversized_files": _PATHS,
        "test_r8_no_file_already_there_is_replaced_or_deleted": _PATHS,
        "test_r7_rule1_a_directory_goes_only_when_it_is_real_and_empty": _LINK,
        "test_r7_rule2_a_file_goes_only_when_it_is_a_marked_regular_file": _LINK,
        "test_r9_a_symbolic_link_is_never_written_or_replaced": _LINK,
    },
    "test_installer_plan.py": {
        "test_r7_uninstall_gives_no_command_for_a_directory_that_holds_data": _PATHS,
        "test_resolved_state_locations_are_pinned_for_the_service": _PATHS,
        "test_uninstall_says_where_saved_attachments_stay": _PATHS,
    },
    "test_installer_setup.py": {
        "test_source_replaces_whatever_was_recorded": _PATHS,
    },
    "test_personal_provider_cleanup.py": {
        "test_execute_logs_each_header_digest_privately_and_appends_undo": _OPEN,
    },
    "test_rules_store.py": {
        "test_missing_store_holds_no_rules_and_paths_follow_the_journal": _PATHS,
        "test_the_store_must_be_a_private_regular_file[group-readable]": _MODE,
        "test_the_store_must_be_a_private_regular_file[world-writable]": _MODE,
        "test_the_store_must_be_a_private_regular_file[symlink]": _LINK,
        "test_a_fifo_is_refused_without_blocking": _FIFO,
        "test_a_lock_that_is_not_the_stores_own_is_refused_and_left_unchanged": _MODE,
        "test_a_lock_of_its_own_readable_by_others_is_made_private": _MODE,
        "test_a_link_and_dotdot_spelling_of_another_directory_is_another_store": _DOTDOT,
        "test_a_bound_save_stays_in_its_directory_when_the_link_to_it_moves": _LINK,
        "test_a_bound_save_creates_a_missing_directory_only_at_its_bound_real_path": _LINK,
        "test_a_link_appearing_below_the_bound_directory_is_never_followed": _LINK,
        "test_a_link_in_the_resolved_directory_is_never_followed": _LINK,
        (
            "test_a_directory_missing_at_preview_binds_the_deepest_existing_one"
            "_and_saving_creates_it"
        ): _LINK,
        "test_a_bound_save_writes_only_into_the_directory_it_checked": _HELD,
        "test_a_bound_save_creates_its_missing_directory_only_in_the_parent_it_checked": _HELD,
        "test_a_bound_save_keeps_to_a_directory_made_since_preview": _HELD,
        "test_a_bound_directory_removed_after_the_check_refuses_the_save": _HELD,
    },
    "test_rules_timer.py": {
        "test_state_and_lock_must_not_alias_action_journal[symlink]": _LINK,
    },
    "test_rules_timer_safety.py": {
        "test_unsafe_state_or_lock_never_touches_mail[symlink-state_path]": _LINK,
        "test_unsafe_state_or_lock_never_touches_mail[symlink-lock_path]": _LINK,
        "test_unsafe_state_or_lock_never_touches_mail[fifo-state_path]": _FIFO,
        "test_unsafe_state_or_lock_never_touches_mail[fifo-lock_path]": _FIFO,
    },
    "test_saved_rules.py": {
        "test_a_link_and_dotdot_spelling_of_another_store_cannot_redeem_its_token": _DOTDOT,
        "test_a_store_link_retargeted_during_confirmation_refuses_and_keeps_the_token": _LINK,
        "test_a_store_link_retargeted_during_the_save_cannot_move_it": _LINK,
        "test_a_missing_store_directorys_parent_replaced_during_the_save_cannot_move_it": _HELD,
    },
    "test_security.py": {
        "test_keychain_uses_fixed_service_account_and_timeout": _PATHS,
    },
    "test_thread_seed_index.py": {
        "test_seed_uid_unavailable_or_unsafe_cache_returns_no_hint[permissions]": _MODE,
    },
}


@functools.cache
def _can_make_links() -> bool:
    """Real symbolic links: they need a privilege on Windows, and Wine only pretends to."""
    with tempfile.TemporaryDirectory() as folder:
        link = os.path.join(folder, "link")
        try:
            os.symlink(folder, link, target_is_directory=True)
        except OSError:
            return False
        return os.path.lexists(link)


def skip_reason(test_id: str) -> str | None:
    """Why a Windows run leaves out this test, from its "file::test[case]" id, if it does."""
    file, _, test = test_id.partition("::")
    entry = _SKIPS.get(file, {})
    reason = (
        entry if isinstance(entry, str) else entry.get(test) or entry.get(test.partition("[")[0])
    )
    return None if reason == _LINK and _can_make_links() else reason


def has_mode(path, mode: int) -> bool:
    """A path's exact POSIX mode; Windows has none to check."""
    return sys.platform == "win32" or stat.S_IMODE(os.stat(path).st_mode) == mode
