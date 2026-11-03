# Changelog

Notable changes to proton-mail-mcp. Entries describe behavior visible to MCP clients.

## Unreleased

### Changed

- **The Claude Code plugin moves to a new release after its upload.** The plugin's lock may now
  name only PyPI's files, and a release reaches PyPI only with the upload, so a release leaves the
  plugin on the version before it until a follow-up bumps it. `scripts/check_plugin_lock.py`
  refuses a lock with any other source in `make check`, in each Checks job and in the release
  sync, so a lock the Claude directory reports as "Launcher lock invalid" no longer reaches the
  release repo.
- **The privacy policy is its own page, `PRIVACY.md`.** It says what the server reads, what it
  keeps where on Linux, macOS and Windows and how to delete it, and every network connection,
  including that sending uses Bridge's SMTP on this computer only, even with a remote Bridge, and
  that setup runs a client's own `mcp list`, which checks every server that client has. The
  plugin's `privacyPolicyUrl` and the bundle's `privacy_policies` link it; the README's Privacy
  Policy and Network connections sections now point there.

### Fixed

- **The Claude Code plugin's lock names PyPI's files for 1.30.1.** `plugin/uv.lock` still recorded
  `mcp-proton-mail` 1.30.1 from the files `uv build` wrote to `../dist` before the upload, so the
  Claude directory's validation reported "Launcher lock invalid". It now names PyPI's sdist and
  wheel with their hashes, like every other package it locks; no other package changed.

## 1.30.1

### Added

- **Every tool says what it does, when to use it and what comes back, and has a title.** Each
  description now opens with its purpose, then says when to use it rather than a sibling tool,
  what it changes, needs and refuses, and what it returns, within the 2,048 characters some clients
  keep; the arguments the model fills in most, such as `folder` and `to`, give examples. Every tool
  also has a title and declares all four hints. Reads are `idempotentHint=true` and previews
  `idempotentHint=false`, because each issues a new token; of the writes only `create_folder`,
  which leaves an existing folder alone, is `idempotentHint=true`. Which tools are read-only, and
  so which calls the audit records, is unchanged, and so are every approval, form and refusal.
  With sending off, the server instructions now say that no tool sends mail.
- **`proton-mail-mcp introspect` and a Dockerfile for registries.** Glama, and registries like it,
  check a server by starting it in a container and listing its tools over stdio. `introspect`
  answers `initialize` and `tools/list` with the default tools and server instructions, sending
  off, and refuses every tool call before it starts: it reads no settings or credentials, reaches
  no Bridge and writes no file. The repository's `Dockerfile` builds it from the checkout and runs
  it as a non-root user from inputs pinned for every build of a commit: the base image by digest
  and each runtime package at the version the tests ran with, in `docker/constraints.txt`.
  `scripts/docker_smoke.sh` builds the image and checks those answers with no network and a
  read-only filesystem. Neither changes how the server runs anywhere else.
- **The README lists every network connection**, under
  [Network connections](README.md#network-connections): the relay to the service and the service
  to Bridge on `127.0.0.1`, and approved unsubscribe links, while the server runs; PyPI, which
  `uvx` checks each time a client starts the Claude Desktop bundle or the Claude Code plugin, but
  not a client set up by `setup`; and PyPI, uv's Python and Bridge's sources during install and
  setup.

### Changed

- **The install runs the package from PyPI by name.** `mcp-proton-mail` is on PyPI, so the README
  now starts with `uvx mcp-proton-mail setup`. The README, `llms.txt`, `llms-install.md` and the
  install, Windows and client pages drop the "Not on PyPI yet" warning and the clone with
  `uvx --from .`; a checkout is now only for development, as
  [Running from a checkout](docs/install.md#running-from-a-checkout) says. `SECURITY.md` supports
  the latest release on PyPI beside `main`, and the CI page no longer warns that the name could
  still be taken.
- **The homepage is https://manajev.com/proton-mail-mcp/.** The package's Homepage link, the MCP
  Registry entry's `websiteUrl`, and the Claude Desktop bundle's and the Claude Code plugin's
  `homepage` point there, and the package's Documentation link points at `docs/` in the public
  repository.
- **The Claude Desktop bundle and the Claude Code plugin run 1.30.1**, the bundle as
  `uvx mcp-proton-mail@1.30.1 connect` and the plugin as `uvx mcp-proton-mail==1.30.1 connect`,
  the exact pin the Claude directory asks for. `plugin/uv.lock` records the dependencies that
  resolves to, for the directory's review; uvx does not read it. The plugin's README starts
  setup with `uvx mcp-proton-mail setup` instead of a clone.
- **`--version` names the package as PyPI does**, `mcp-proton-mail 1.30.1`, under either of the
  command's names.

## 1.30.0 - 2026-11-02

### Fixed

- **Views read their data again when the host leaves it out.** Claude Desktop passes a view only a
  result's text, without the view's data in `_meta`, so the thread, composer and send review stayed
  on "Loading…". A view without its data now calls its own view tool once more through the host,
  with the arguments the host delivered, and renders that result, even when the host does not
  declare `serverTools`; a failed read says why and offers Retry. This should let the views load in
  Claude Desktop, which is not yet confirmed in a live Claude Desktop session. `show_thread`,
  `compose_reply` and `review_send`, which only read and issue no token, are now callable from their
  views (`visibility: ["model", "app"]`); every other tool except `create_draft` stays model-only.
- **Requests to see or answer mail open the views.** `show_thread` now says first that it is the
  default for a request to show, open, read, look at or display mail, and `compose_reply` for a
  request to reply, write back or compose. `read_thread` starts by saying it is text-only, for
  analysis without a view, and `create_draft` that it is the no-view path; the server instructions
  say the same.
- **A draft's Message-ID uses its From address's domain.** It ended in the computer's host name,
  which on a Mac could be a reverse-DNS `ip6.arpa` name holding its network address. It now ends in
  the From domain, or `proton-mail-mcp.invalid` when that is not a plain DNS name.
- **Long messages are read whole up to 256 KB.** `read_thread`, `show_thread`, `compose_reply`'s
  quote and `forward_message` read 262,144 text bytes per message instead of 65,536, so an 80 KB
  newsletter is no longer cut. Longer messages still report `truncated=true`; the time limits are
  unchanged.
- **Dates are shown in the user's time zone.** Bridge writes `Date` headers in UTC, which the
  model then quoted. `read_thread`, `show_thread` and `search_mail` messages now add `date_local`,
  ISO 8601 with the offset of the new `PROTON_MCP_TIMEZONE` setting or of this computer's zone,
  and `compose_reply` quotes the original's date in that zone. Views keep showing dates in the
  host's or the browser's zone, and now also read a `Date` header that ends in a comment.
- **Setup shows what it will do before it asks anything.** On a terminal it lists its steps and what
  only you can do, then asks for the Bridge username and the attachments folder, then prints the
  full plan.
- **Setup stops for a running Bridge app.** It installed its own Bridge service beside the desktop
  app, which then could not start. Now the app's window, or a Bridge port held while the
  installer's own service is not running, stops the plan before any change. On a terminal, setup
  waits for you to quit the app, or keeps it with `--skip-bridge-service`.
- **A stored Bridge password skips Bridge's login.** Setup no longer offers Bridge's CLI when a
  Bridge password is already stored. Its note gives the commands to log in again by hand, which
  keeps the token; `--replace-credentials` also stores a new password and a new token.
- **The token hint no longer names commands that print the token.** It named
  `security find-generic-password ... -w`, `secret-tool lookup` and `pass show`. It now names
  `configure-client`, `connect` and `show-token --copy`.
- **The service's virtual environment no longer depends on uv's cache.** It was made from the
  Python `uvx` ran, which can lie in uv's cache, so `uv cache clean` broke the service. It is now
  made from the run's base Python, followed out of the cache through its links. If none leads out,
  setup stops and asks for the new `--python`. A rerun reports an existing environment made from a
  cached Python.
- **A stale `com.proton-mail-mcp.plist.disabled` is reported.** launchd ignores it, but it may hold
  an old token, so the plan names it with the command that removes it.
- **The package has one version, which the server reports.** The wheel said 1.0.0, while the
  server's `serverInfo` reported 1.30.0, the MCP SDK's version, because none was given. Both now
  come from `proton_mail_mcp.__version__`, set to 1.30.0.
- **The install docs start from a clone of the repository**, with `uvx --from .`, not from PyPI,
  where the package is not published yet. Installing from PyPI and a listing in the MCP Registry
  are marked as later.
- **Bridge's login works where its window cannot.** On a host without a desktop, the app's
  human-verification step could leave it waiting. Setup now runs Bridge's CLI through a
  pseudo-terminal: you type your email, password and code there, open any verification link in a
  browser, and setup types `info` and `exit` and reads your IMAP username, keeping only that line.
  Where no username is known yet, it logs in first instead of asking. A username that differs from
  the one given stops the run before anything is stored. Windows keeps the plain CLI.
- **Pasted answers work.** The Bridge username and password prompts drop the marks some terminals
  put around a paste and the spaces and line break a copied line brings along.
- **Without a Bridge username, an unattended run hands over the login.** `--yes`, `--json` or no
  terminal used to stop with "Pass --bridge-user"; it now stops with a `bridge_login` hand-off and
  the command a person runs in a terminal.
- **Hosts without systemd get a running server.** Setup used to set up nothing there and skip its
  self-check. It now starts Bridge and the MCP server as background processes, with pid files in
  the state directory, runs the self-check, and on a rerun restarts the server, by its pid only,
  when something it reads changed. Uninstall names those processes.
- **A missing `secret-tool` no longer ends a dry run.** The dry run shows the whole plan with a
  `store_tools` hand-off; a real run still stops first.
- **A D-Bus session without a Secret Service is found first.** Where `dbus-send` can ask, setup
  stops before any change with a `secret_service` hand-off, instead of failing at the first store.
- **A failed `apt-get update` no longer stops the Bridge install** on Debian and Ubuntu. The install
  docs give a manual path, and the dependencies a minimal image lacks.
- **A stale token file is named.** A `token` file left in the settings folder by an earlier manual
  setup gets `401` once setup stores a new token; setup names it, with the command that deletes it.
- **The search index waits out Bridge's first sync.** While Bridge is still downloading the mailbox
  after a login, recovery failures no longer use up its retries and end in `rebuild_failed`;
  results report `bridge_syncing` and say the index catches up by itself.
- **An unattended run never lets sudo ask.** `--json` and `--non-interactive` run `sudo -n`, so a
  sudo login that runs out mid-run fails with a hint to run `sudo -v`, instead of prompting.
- **`--python` takes a name on `PATH`** and is checked to be Python 3.12 or newer before planning.
- **Installer options must be spelled out.** argparse took an abbreviation such as `--js` for
  `--json`, but then a usage error was not printed as JSON. Abbreviations are now refused, and
  `--json=1` gets its usage error as JSON.
- **A bad saved setting is named.** A saved `PROTON_MCP_PORT` or virtual environment path that is
  not valid used to be blamed on `--port` or `--venv`, which were never given; the refusal now
  names the setting and the env file.
- **A stopped Bridge is named.** When nothing answers on Bridge's IMAP address, tools said
  "Mailbox operation failed; check Bridge configuration and availability". They now say
  "Proton Mail Bridge is not running: nothing answers on 127.0.0.1:1143", with how to start it.
  Other backend errors still give no details.
- **Invisible preheader filler no longer fills `read_thread`'s text.** Marketing mail pads its
  preheader with long runs of zero-width and other invisible characters. Each run of two or more
  becomes one space in `read_thread` and `show_thread` text; a single one, as in an emoji
  sequence, stays.
- **A message sent to yourself now has a canonical handle.** It is listed in both INBOX and Sent,
  which the lookup treated as copies it could not tell apart, so `read_thread` and `show_thread`
  returned null `folder`, `uidvalidity` and `uid`, and a search of All Mail dropped the row. Those
  tools now report the INBOX copy. A message still in several other real folders stays null.
- **`read_thread` and `show_thread` now say why a message has no canonical handle.** A message with
  null canonical fields carries `canonical_reason`: `no_single_copy`, `lookup_unfinished` or
  `lookup_skipped`.
- **A text search over a wide date range can now be followed to its end.** In a folder of more than
  2,000 messages, any `since` or `before` range is read message by message, not only one of at most
  500, and the cursor holds one number for the UIDs finished, so a long scan no longer ends with
  `no_resumable_cursor` after about 230 messages. A stopped scan's pages follow UID order, each
  sorted newest first. A last page that keeps `text_scan_limited` with a null cursor now has a `hint`
  saying that every message was read and at least one could not be read in full.
- **`read_thread` and `show_thread` no longer warn when a canonical lookup cannot finish.** The
  fields stay null, and that alone no longer sets `partial` or `partial_reason`, or shows a banner.
  All Mail rows stored without a date, envelope or size are now fetched, because one of them kept
  every later lookup out of the index, and a live scan that could not fit the 3-second cap is no
  longer started above 5,000 All Mail messages.
- **Body-text search no longer times out on a narrow date window in a large folder.** One `TEXT`
  key makes Bridge read every message in the folder, whatever dates share the command, so narrowing
  the dates never helped. In a folder of more than 2,000 messages, a date window is
  now read one message at a time with `BODY.PEEK[]`, up to 1 MiB each, and compared here the way
  Bridge compares `TEXT`. Nothing is marked read and no message text is stored. The new
  `partial_reason` `text_scan_limited` reports a window that ran out of time, with a cursor while its
  rows fit one, or a message left undecided, up to the last page. Following the cursor moves on
  every call or ends with `no_resumable_cursor`. A read that stalls with its whole wait available
  is skipped and named in `skipped_uids`. A read cut short by the call's budget is read again. Each
  read also brings its row's date, flags and headers, so a cut read keeps the rows found, even with
  no time for a new connection, and the cursor resumes at that read. A search with no dates still
  goes to Bridge and can time out. `count_mail` still goes to Bridge, so its `text` description and
  its timeout error now point to `search_mail` instead of to narrower dates.
- **`read_thread` and `show_thread` finish their canonical lookup and expansion after new mail
  arrives.** New mail made All Mail's and the holder folder's proofs stale, which sent the read to
  scans that could not fit its limits: a 3-second cap on a day scan of each folder, and a 10-second
  command limit on a header search of All Mail. A read with expansion now catches All Mail up first.
  A changed real folder confirms its cached rows by `UID FETCH`, and reads the UIDs that arrived
  since its last proof only for a message still without a copy. If the index still cannot help a
  mailbox of more than 2,000 messages, expansion follows cached links and reports
  `thread expansion limited` instead of a search that would time out. These numbers come from tests
  with fake servers and have not been measured on a live Bridge.
- **`list_actions` accepts plain dates.** `since` and `before` also take `YYYY-MM-DD`, read in the
  server's local timezone. A date covers that whole day in either bound, so `before` includes it,
  unlike `search_mail`'s `before`. Timestamps without a timezone are still refused.

### Added

- **A security policy.** [`SECURITY.md`](SECURITY.md) says how to report a vulnerability
  privately, what is in scope and when to expect an answer. The repository also has a fuller
  contributing guide, a code of conduct, issue and pull request templates that ask for redacted
  details, and, in the public repository, Dependabot updates for the Python dependencies and the
  workflows' actions, which are now pinned to full commits.
- **A workflow that publishes a tagged release to PyPI.** In the public repository only, a `v*` tag
  or a published GitHub release on one builds the sdist and wheel, checks that the tag, the files
  and `server.json` name one version, and uploads them with PyPI's Trusted Publishing, so no token
  is stored. Nothing is published until PyPI trusts the workflow and a release is tagged; the MCP
  Registry listing stays a step by hand. [CI](docs/ci.md#publishing-to-pypi) has the setup.
- **`proton-mail-mcp connect`.** A local stdio MCP server that relays every message, unchanged, to
  the installed service, and reads the bearer token from the credential store each time it starts,
  so a client's config holds no token. It replaces `npx mcp-remote` for Claude Desktop, so no
  Node.js is needed. A service that is down, or takes the connection and does not answer
  `initialize` within a minute, a refused token or a missing one reaches the client as a JSON-RPC
  error that says what to do; a restarted service gets its session set up again. A
  `--token-file` that is not a regular file, such as a folder or a named pipe, is refused at once.
- **Setup adds the server to seven MCP clients.** `setup --client` and the new `configure-client`
  take `claude-desktop`, `claude-code`, `chatgpt`, `codex`, `gemini`, `cursor` and `grok-bot`.
  Every client but ChatGPT starts connect. Claude Code, Codex and Gemini CLI get it through their
  own `mcp add` when their command is on `PATH`, then `mcp list`; a failed `mcp add` never loses
  an entry that was there. ChatGPT gets a direct HTTP entry, with the token, in
  `~/.codex/config.toml`, which Codex also reads: Codex keeps that entry rather than replace it,
  and ChatGPT replacing a Codex entry warns. Grok Bot, or another agent, gets the entry shown to
  add. Each change keeps every other server, the file's line endings, and in TOML the comments
  outside the replaced table; it keeps the old file as a dated `.bak`, writes owner-only and says
  when it replaced an entry; an unchanged entry leaves the file alone. A dry run shows the change
  without any value already in the file, and a file a run could not edit safely refuses the plan
  before any step runs, in a dry run too. The token is read only for ChatGPT, only when applied,
  and never printed.
- **`--print-config`** prints each client's entry, its own `mcp add` command and, for Cursor, a
  one-click link, without reading the token. Cursor and VS Code install buttons are in the docs.
- **`--json` and `--non-interactive` for agents.** `setup`, `install`, `uninstall` and
  `configure-client` then ask nothing and open no prompt. `--json` prints one document with each
  step's status and `needs_user`: what only a person can do, when, whether it is `blocking`,
  whether it stops this run, or in a dry run would stop the same run with `--yes`, and the command
  to hand over. That command, and `launcher`, start the same copy, through `uvx --from <source>`
  when the run came from uvx. `connector` gives the command any client can start, without the
  token. A usage error is a JSON document too.
- **`show-token --copy`** puts the token on the clipboard without printing it, at a terminal only.
- **Files for the MCP Registry, Claude Desktop and Claude Code, published nowhere.** `server.json`
  and the README's `mcp-name` line describe the package for a later registry listing. `make mcpb`
  builds a Claude Desktop bundle, in the shape Smithery's publisher accepts, whose command is
  `uvx mcp-proton-mail@<version> connect`: it holds no copy of the package, on macOS runs it
  through `/bin/sh` with the folders where `uvx` is usually installed in front of PATH, so that an
  app started from the Dock finds it, and has one optional setting, the settings file.
  The repository is also a Claude Code plugin marketplace whose one plugin runs the same command.
  [Claude Desktop](docs/clients/claude-desktop.md#the-mcpb-bundle) says why the bundle uses `uvx`
  under the `python` server type.
- **A privacy policy.** The README says what stays on your computer, what the server connects to,
  what a send needs and when it needs no approval, and what reaches your MCP client's model
  provider.
- **Install docs for agents and a page per client.** `llms-install.md` is the steps an agent
  follows, with the one hand-off of the Bridge login to the person; `docs/clients/` has a page for
  each client.
- **Native Windows support, so far run only on Windows Server runners.** The server, `setup` and `uninstall`
  now run on Windows instead of refusing it. File locks use `LockFileEx`, private state lives in
  `%LOCALAPPDATA%\proton-mail-mcp`, and the action log, saved rules, indexes, attachments folder
  and env file must lie inside the user's profile. Windows has no owner or mode bits to check, so
  the server reads each one's real owner and access rules and refuses if anyone but the user,
  SYSTEM and Administrators has access; it changes no rules itself. Those checks are tested with a
  stand-in security API and, on real Windows, only in CI on an elevated Windows Server runner, not
  on Windows 10 or 11, from a standard account or against a second account. On Linux, `strace`
  shows the same file calls as before; macOS takes the same code; only its installer jobs have run.
  On Windows it has not met
  Windows 10 or 11, a normal account or a real Proton account. See
  [docs/windows.md](docs/windows.md), which lists what that does not cover.
- **Typed OK on clients with no form.** A client whose `initialize` capabilities omit
  `elicitation` used to be refused at `send_draft`. Now, with the new
  `PROTON_MCP_SEND_TYPED_OK` on, which is the default, `preview_send` reports
  `approval: "typed_ok"` for it, with a plain-text `summary` saying this app has no
  confirmation form, so type OK to send, and the assistant calls
  `send_draft(token, draft_id)` after the user typed an explicit OK in chat. This covers
  every send, including external recipients. **The server cannot prove that a human typed
  the OK**: the token only guards against a changed draft and a replay. Each preview and
  send on this path logs a WARNING with the client's name and version, never the token.
  `PROTON_MCP_SEND_TYPED_OK=0` restores the refusal. `send_draft` gains `draft_id`, which
  can only refuse, and a token from another draft refuses. A client that declares
  elicitation always gets the form and never falls back. A client declaring an empty
  `elicitation: {}` counts as having a form, as the MCP specification says, and now gets
  one instead of a refusal, for mailbox changes too. See
  [Typed OK](docs/sending.md#typed-ok-when-the-client-has-no-form).
- **Typed-OK client list and confirmation logs.** The new
  `PROTON_MCP_SEND_TYPED_OK_CLIENTS`, a comma list of `clientInfo.name` values, empty by
  default, treats a listed client as having no form even if it declares elicitation, for a
  host that declares it and never shows the form. Listing a client is the operator's choice:
  the name is self-reported, the same trust level as the elicitation flag. Sends on a listed
  client take the typed OK, its mailbox changes that need a form refuse at once, and
  `PROTON_MCP_SEND_TYPED_OK=0` overrides the list. A decline, cancel or timeout on a real form
  stays refused. The service log now records at INFO each session's client name, version and
  whether it declared elicitation, why a form gave no accept, and each typed OK that only the
  list opened, with no mail content, addresses or tokens. The typed-OK WARNING now masks an
  address-shaped client name too. The setup report names the listed clients and says the names
  are self-reported, or says the list is ignored under `PROTON_MCP_SEND_TYPED_OK=0`. In the env
  file, write names without quotes: a quoted name keeps its quotes and never matches.
- **Attachment tools.** `list_attachments` lists a message's attachments;
  `read_attachment` returns text from a PDF, TXT or CSV, or an image as image content, up
  to 10 MiB (images 5 MiB); `save_attachment` saves one, up to 25 MiB, as a new private
  file. Local files are used only inside the folder set by the new
  `PROTON_MCP_ATTACHMENTS_DIR`, a normalized absolute path with no trailing slash: absolute
  paths, `..`, hidden names and links are refused, and nothing is overwritten. PDFs are parsed in a separate process with no environment,
  using the new `pypdf` dependency. See [Attachments](docs/drafts.md#attachments).
- **Attachment tools work without configuration.** With `PROTON_MCP_ATTACHMENTS_DIR` unset
  they now use `$XDG_DATA_HOME/proton-mail-mcp/attachments` when `XDG_DATA_HOME` is
  absolute, else `~/.local/share/proton-mail-mcp/attachments`, instead of refusing. The
  first save creates that folder with mode `0700`. A folder you set still has to exist, and
  the single-folder rule, no overwriting and the 25 MiB cap are unchanged. `setup` shows
  the folder in use and changes it, at its question or with `--attachments-dir`. Uninstall
  leaves the folder and its closing note names it. Errors about the folder name the setting.
- **Sending from an alias.** `create_draft`, `forward_message` and `compose_reply` take
  `from_address`, which must be exactly on `PROTON_MCP_SEND_FROM`; `list_from_addresses`
  lists the allowed addresses. An alias draft takes the same preview, form and send path.
  The form and review mark it, previews gain `from_alias`, and it skips the form only when
  every recipient is the user's own under `PROTON_MCP_SEND_SELF_NO_FORM=1`. The
  composer offers the allowed addresses and a reopened draft keeps an allowed alias.
- **The From address and attachments are on screen before approval.** The send form's
  first line names the From address and each attached file's name, type and size. A name
  keeps its own letters, and invisible characters in it show as `_`. The send review always
  has an **Attached** row, the composer has a **From** row, and the thread view shows each
  attachment's name, type and size and offers **Forward**. These views wrap a long name
  rather than cut it, so its real extension stays on screen. Preview attachments gain `type`.
- **`forward_message`.** Saves an unsent draft forwarding one message with only the
  attachments listed by index, a required choice. It refuses when the original's text
  cannot be read. It has no send path of its own:
  `preview_send`, the host form and `send_draft` send it. Drafts with attachments or a
  forward subject never take the own-address path without a form.
- **Attachments on drafts.** `create_draft` takes `attachments`, files inside the allowed
  folder, and `attachments_from` with `attachment_indexes`, attachments copied from a saved
  draft or message, up to 20 files and 7 MiB per draft. They reach the send preview and
  form like any draft's attachments, including a non-ASCII name in the form Bridge stores
  it, `filename="=?utf-8?q?...?="`. `compose_reply` now keeps a reopened draft's
  attachments, where saving an edit used to leave them out, and a saved draft's in a reply.
  It lists a reply's original attachments for the user to tick, and takes `attachments`
  in any mode. `not_copied` now lists only Bcc recipients.
- **`pipx install mcp-proton-mail` or `uvx mcp-proton-mail`, then `proton-mail-mcp setup`.**
  The package is version 1.0.0 with complete metadata and an Apache-2.0 `LICENSE`. The
  `proton-mail-mcp` command now dispatches: with no arguments or `serve` it starts the
  server as before, while `setup`, `uninstall`, `show-token` and `detect` run the installer
  from the wheel with no checkout. Anything else prints usage and exits 2 instead of
  ignoring the arguments and starting the server. Services keep running
  `proton-mail-mcp-service`. `setup` asks for the Bridge username, offers Bridge's CLI for
  the login (on a rerun, it offers to stop and restart the installer's own Bridge service
  around it, and never opens the CLI beside a running Bridge), stores the Bridge password
  from a hidden prompt, installs the services, and finishes with a self-check through the
  running server (no token refused, stored token accepted, `INBOX` readable through
  Bridge). The service's private environment gets the same copy: a local wheel or
  directory by its path, and a copy installed by name by its pinned version, with any
  index options pipx or `uv tool` recorded. The installed files must then match the
  running copy, or `setup` stops before touching a service and asks for `--source`.
  Service units and LaunchAgents are package data, as are the examples. On Cygwin and MSYS2,
  `setup` exits 2 and points to Python for Windows or WSL2.
  `scripts/wheel_smoke.py` (`make wheel-smoke`) proves it all
  from a built wheel with stand-ins for `systemctl`, `pgrep`, `secret-tool` and Bridge. See the
  [installer guide](docs/install.md#quick-start).
- **Clear index-rebuild status in tool replies.** Search, count and read results
  limited by incomplete header-index coverage include a plain `index_note`, such as
  "search index rebuilding: 250/1000 messages indexed (25%); results may be incomplete",
  and shared `index_status` fields: `state`, `indexed_count`, `target`, `percent`
  and `eta_seconds`. Complete results, exact IMAP counts and reads fully resolved
  through live IMAP omit these fields. Progress uses the affected mailbox's
  observed message total, reports unknown target/percent as `null`, treats an
  empty target as 100%, and leaves ETA `null` without an observed rate for that
  result's scope.
- **Automatic startup header-index recovery**, enabled by default and independent
  of `PROTON_MCP_BACKGROUND_SYNC_ENABLED`. A discarded pre-v2, empty or incomplete
  cache now recovers All Mail and real-folder headers without requiring a search
  or manual rebuild. The read-only worker uses a dedicated preemptible connection,
  bounded batches and the serving cache, preserving completed work across restarts.
  Set `PROTON_MCP_INDEX_AUTOREBUILD_ENABLED=0` to disable it. Journal messages record
  start, progress and finish. See
  [automatic startup recovery](docs/search-index.md#automatic-startup-recovery).
- **Optional background All Mail sync**, off by default
  (`PROTON_MCP_BACKGROUND_SYNC_ENABLED`, `PROTON_MCP_BACKGROUND_SYNC_INTERVAL`,
  `PROTON_MCP_BACKGROUND_SYNC_MAX_COMMANDS`). When
  enabled, one read-only thread polls All Mail with STATUS every 15 minutes and fetches
  only new headers for an index that already exists. It never builds an empty cache,
  waits for a tool call after every start, gives way to every tool call by closing
  its own socket, stays inactive while the IMAP IDLE watcher is on, and by default spends at most
  1500 IMAP commands and 5000 header rows per UTC day. It also keeps the
  `preview_rules` raw audit of All Mail in memory for up to 24 hours instead of 10
  minutes, and a later preview reads only the new UIDs of a mailbox that only grew.
  `reindex_status` reports it in a `background_sync` object while enabled. See
  [the search index guide](docs/search-index.md#background-all-mail-sync).
- **Installer for Linux and macOS.** `scripts/install.sh` (Debian/Ubuntu,
  Fedora/RHEL, Arch, macOS) runs the standard-library
  `proton_mail_mcp.installer`: OS detection, Bridge from official sources with
  signature checks (`debsig-verify` with temporary policy and keyring
  directories holding only Proton's policy and key, and `rpm --checksig` against
  a temporary rpm database holding only Proton's key, both with the key's full
  fingerprint pinned; Arch `[extra]`; the Homebrew cask plus code-signature and
  Gatekeeper checks), the Bridge password and a generated bearer token in the OS
  credential store, and per-user systemd units or LaunchAgents for Bridge and the
  MCP server on `127.0.0.1`. Plans print with `--dry-run`; reruns are idempotent;
  the env file pins the audit log and index locations the plan resolved; a
  virtual environment the installer did not create is never reused (`--venv`
  picks another path); the installer never deletes a directory with content:
  a previous virtual environment, the current one and the download directory
  stay, and every run prints, and appends to `installer.log` in the state
  directory, their size and, where it cannot obviously harm the installation,
  the command that removes them by hand; it reads no service definition it did
  not write; on a host that does not run systemd (a container, Artix) the plan
  goes on without services and says so, while a systemd host whose user manager
  cannot be reached stops before any change unless `--skip-services`;
  with `pass`, the store directory and a custom GnuPG home are pinned in the env
  file for every `pass` call of the service; systemd units are enabled by path, so a
  user manager with another `XDG_CONFIG_HOME` still finds them; `uninstall` stops
  only installer-owned services, proves each one stopped with a separate check
  (for launchd, only "service not found" counts), and only then removes the marked
  files it wrote (units or LaunchAgents and the settings file, which stays unless
  the installer's own MCP service was stopped and checked); it changes nothing and
  prints the manual steps while it cannot control the installer's units, or while a
  definition it did not write, or one the service manager does not name, holds one
  of its service names, and keeps state, credentials and Bridge unless asked. A
  download or temporary file never replaces a file already there, a `--force`
  backup never replaces an earlier one, a settings file the installer did not
  write is neither used nor shown, and the signature checks' scratch directories
  stay. The
  installer never enables send, never turns off confirmation, never reads the
  Bridge password with echo and never installs the rules timer. See
  [the installer guide](docs/install.md).
- `PROTON_MCP_CREDENTIAL_STORE` selects where the Bridge password is read:
  `keychain` (default and unchanged), `libsecret`, `pass` or `wincred`.
  `PROTON_MCP_PASSWORD_STORE_DIR` and `PROTON_MCP_GNUPGHOME` locate a `pass`
  store and its GnuPG home.
  `proton-mail-mcp-service --env-file` starts the server from a non-secret
  settings file and loads the bearer token from the same store.
- GitHub Actions `Installer` workflow: installer unit tests and dry runs on
  hosted Ubuntu and macOS runners and in Fedora, AlmaLinux 9 and Arch
  containers; on Windows, the installer tests and dry runs.

- **MCP Apps views.** The server implements the MCP Apps extension
  (`io.modelcontextprotocol/ui`, specification `2026-01-26`). View tools link a
  self-contained `ui://` HTML resource (`text/html;profile=mcp-app`) through
  `_meta.ui.resourceUri`; the documents inline their styles and scripts, declare
  no CSP domains and load nothing at runtime. Clients without
  MCP Apps support receive the ordinary text result. Sanitized message HTML
  travels only in the result's `_meta`, which MCP Apps hosts keep out of model
  context; reply fields, quoted text and the other mail text a view shows are
  also in the ordinary text result, which the model reads. There is no
  `structuredContent`.
  Views may call only `create_draft`; every other tool,
  including the mail-reading view tools, is marked
  `_meta.ui.visibility=["model"]`, so hosts refuse view calls to it: views
  never obtain or redeem approval tokens and read no mail beyond the result
  they show. `PROTON_MCP_APPS_ENABLED=0` restores the previous tool list. See
  [MCP Apps views](docs/mcp-apps.md).
  - `show_thread(message_id, include_thread=True)` renders a read-only thread
    view from the existing bounded `read_thread` read, preferring HTML parts.
    Message HTML is re-serialized from an allowlist (scripts, handlers, forms,
    frames and remote images, styles and fonts removed and counted), then shown
    in a sandboxed frame without script permission under `img-src data:`. A link
    opens only after the view shows its real destination (host name and full
    URL, never the sender's link text or title) and the user confirms; links
    work from the keyboard, and opening, closing or switching a message between
    formatted and plain text keeps the keyboard focus on its control. The text
    result keeps `read_thread`'s shape with text extracted from HTML, plus a
    hexadecimal `view_ref` per message that the view's Reply message quotes
    instead of any sender-chosen text.
  - `compose_reply(message_id=None, draft_id=None, reply_all=False)` prefills a
    reply (To from Reply-To or From, optional reply-all Cc without this
    account's addresses, `Re:` subject, In-Reply-To and quoted text) or reopens a
    saved draft. Its composer view saves each version through the existing
    `create_draft` tool and never sends or deletes anything. When a reopened
    draft has attachments or Bcc recipients, which `create_draft` does not
    write, the text result lists them in `not_copied`, and it and the composer
    warn before anything is saved that the new draft leaves them out. Only a
    message flagged as a draft reports its Bcc; sent mail never does.
  - `review_send(draft_id)` (send enabled only) runs every `preview_send` check
    and returns the same preview from the same code path plus the draft's text,
    without issuing a token. Its view's **Send…** only asks the assistant to run
    `preview_send` and `send_draft`, so the single-use token and host
    confirmation still gate every send.

- **Background All Mail header rebuilds.** `reindex_preview`, `reindex_execute`,
  `reindex_status` and `reindex_cancel` build a private candidate while ordinary
  searches use the old index. Preview tokens last five minutes and are single-use;
  the header-only, read-only worker requires at least 99% current UID coverage
  for an atomic swap. A successful swap replaces the cached headers; ordinary
  reads warm any omitted folder caches again. Startup
  removes stale candidates under exclusive nonblocking locks and records
  interrupted runs.
- `PROTON_MCP_SEARCH_INDEX_CATCHUP_MAX_MESSAGES` bounds header catch-up on an
  unbounded first All Mail search page (default `2000`, integer from `0` to
  `10000`; `0` disables catch-up fetches). Dated searches and cursor pages skip
  catch-up. It reserves time for the normal 30-day fallback; search responses
  describe result limitations through `partial` and `partial_reason`.

- **Optional IMAP IDLE new-mail watcher.** Enable with
  `PROTON_MCP_IMAP_IDLE_ENABLED=1`; `PROTON_MCP_IMAP_IDLE_FOLDERS` selects a JSON
  array of watched folders (default INBOX). Dedicated read-only connections keep
  tool calls independent, renew IDLE before 29 minutes, reconnect with bounded
  backoff, and fall back to configurable NOOP checks if IDLE is unavailable.
  New headers update the header index incrementally; expunges and UIDVALIDITY
  changes remove stale entries. Watching stops with the server and never acts on
  mail. See [IMAP IDLE search index updates](docs/imap-idle.md).

- **Incremental SQLite header search index.** `search_mail` can skip known header
  nonmatches using a private account/endpoint-specific cache beside local state.
  Decoded subjects and From/To/Cc names and addresses are indexed, along with
  dates and membership metadata; no bodies are stored or fetched for the index.
  Entries use folder, UIDVALIDITY and UID, update incrementally, and are removed
  on source moves, expunges, complete-inventory reconciliation and UIDVALIDITY
  resets. Missing, unavailable or corrupt
  indexes retain the existing search path with a once-only diagnostic. Search
  contracts are unchanged, and text searches and counts remain Bridge-authoritative.
  The new `proton-mail-mcp-rebuild-search-index --folder INBOX` command explicitly
  rebuilds headers within the normal call budget. See
  [Index storage and rebuild](docs/search-index.md).

- **Truncated-message unsubscribe and guarded mailto drafts.** Unsubscribe
  discovery reads headers independently of displayed bodies and raises the bounded
  HTML fallback scan from 64 KiB to 1 MiB. A permitted link within the bound remains
  usable when the rest is truncated. Header mailto targets prepare unsent drafts
  through the existing draft path and require a separate guarded `send_draft`
  approval. Preview `targets` expose method/host and, for mailto, the exact decoded
  `to`, `subject`, `body` and `requires_send_approval`. Prepared drafts are counted
  as `pending_send`; fully prepared runs use that status in results, the journal
  and audit rather than `partial`. `list_actions(token=...)` recovers their
  `draft_ids` without returning content or tokens. Send-disabled or over-cap mailto
  headers fall through to permitted body HTTPS links. Mailto parsing accepts
  tracking addresses with literal `=` and `/`, and `/` or `?` in field values,
  while retaining field, escape and control-character checks. See
  [Guarded unsubscribe](docs/cleanup.md#guarded-unsubscribe).

- **Saved custom rules (backlog P-E).** `preview_add_rule(name, folder, conditions,
  match, mark_read, include_matches)` validates one folder rule with the Sieve filter
  model of `preview_sieve_filter` (the same conditions, the same LIST and shadow checks
  of the target folder) and refuses a name a saved rule already uses in any letter
  case or Unicode compatibility form. It returns the normalized rule, its
  `rule_sha256`, the same rule as an informational Sieve `script` (for Proton's own
  filter settings), the backend status and a single-use, message-less token bound to
  the rule, both digests, the script and the store target. `include_matches=true` adds
  a read-only count and samples of current INBOX matches from one search and at most
  the 200 newest candidates. `add_rule(token)` refuses before any form when the store
  is full or invalid, asks the user to confirm the exact rule in the client's form,
  checks the binding, the byte-identical script, the folder and the name again,
  journals the intent (digests only), claims the token and appends the rule. Results
  and audits count the rule once with `added`, `attempted` and `rule_sha256`.
  `list_rules()` lists built-in and saved rules in the order they run. See
  [Saved custom rules](docs/rules.md#saved-custom-rules).
- Saved rules live in a private store of their own: `saved-rules.json` beside the action
  journal, or `PROTON_MCP_SAVED_RULES` (new setting), apart from the packaged
  `folder_rules.json`. The file must be a `0600` regular file and holds at most 20
  rules; writes are atomic (a verified temporary file renamed over the store under a
  lock) and keep every existing rule unchanged and in order, and no tool edits or
  removes a rule. A missing store holds no rules; an invalid one fails closed.
  Tokens bind the store's real path (symlinks and `..` resolved) and the deepest
  directory on it that exists, the store's own once it exists: `add_rule` refuses
  before the journal, the token or the store changes when the path now leads
  elsewhere or that directory was replaced, then reads and writes only in that open
  directory or the missing ones it creates below it, so a symlink changed or a
  directory replaced meanwhile cannot redirect the rule.
- `preview_rules`, `apply_rules` and the rules timer run saved rules after the packaged
  rules (the first matching rule wins) through the unchanged guarded move path. A saved
  rule matches the way its Sieve script reads: `from` and `to` by each address, never
  a display name, `subject` by the decoded header, `contains` as a substring and `is`
  as the whole value, case-insensitively, `match` `all` or `any`. Its `mark_read` marks
  its own matches read in `apply_rules`; the timer never marks read.
- `add_rule` saves through the Sieve backend interface of `create_sieve_filter`, with
  the local store as its default backend. The Sieve backend stays unavailable and
  refuses, and `create_sieve_filter` is unchanged.

- **Bulk pack (backlog P-D).** `preview_action` accepts a `limit` above 200 for one
  bounded approval of up to `PROTON_MCP_APPROVAL_MAX_MESSAGES` messages (new setting,
  default 500, from 200 to 1000; `200` disables bounded approvals). Every cleanup
  preview except unsubscribe reports the bound as `approval_max_messages`; the
  token binds the full clean set
  and the bound, and the confirmation form shows it. Confirmation runs in chunks of
  at most 200 messages and returns the usual `resume`/`continuation_token` for the
  untouched remainder, which never widens and keeps the original expiry. Without a
  `limit` above 200, the 200-message cap is unchanged. Earlier releases refuse an
  action log that holds a bounded run ("Cleanup run journal has invalid
  approval"), so do not downgrade while the log holds one. See
  [Bounded approvals](docs/cleanup.md#bounded-approvals).
- `move_to_folder` previews accept `params.create_folder_if_missing` (JSON boolean,
  default false). For a missing `Folders/` destination the preview reports
  `folder_creation: {name, exists: false, create: true}` and creates nothing; the
  one token binds the folder creation and the exact move set. `move_to_folder`
  verifies the approved moves, journals and creates the folder, then moves, and
  reports `folder_creation` (`created`, `exists`, `ready`, `uncertain`; audited as
  `folder_created`, plus `folder_creation_uncertain`, which `list_actions` shows).
  `list_actions` lists the creation with its approval's run, also when filtered by
  `action`, without counting it as a chunk. When no move fits beside
  the creation, the call creates the folder alone and returns a continuation.
  An existing folder is an ordinary move. See
  [Creating the destination in the same approval](docs/cleanup.md#creating-the-destination-in-the-same-approval).
- Cleanup responses include `mismatched` (`mismatched_ids` in compact responses):
  rows a batched command left untouched because they changed before it, or whose
  outcome could not be verified, with a reason. They are never counted as done.

- **Search pack (backlog P-C).** `search_mail` messages include `seen`, the IMAP
  `\Seen` flag. New options: `unread_only` (the flag-only `UNSEEN` key; a message read
  after the search is dropped; JSON booleans only), `exclude_subject` (matched on the
  decoded subject, like `subject`), `exclude_text` (`NOT TEXT`, like `text`) and
  `query`, an `and`/`or`/`not` composition of `from`, `to`, `subject` and `text`
  conditions (at most 20 conditions, 5 levels and 1,000 characters per value). Header
  conditions are still matched locally on decoded headers, adding no Bridge header
  scan. Text conditions become RFC 3501 `NOT`, `OR` and parenthesized keys of the same
  `UID SEARCH`, with each value one escaped quoted string or the command's single
  literal, and control characters refused; a text value that cannot join that command
  costs one more `UID SEARCH`. See
  [Unread mail, exclusions, queries and counts](docs/search.md#unread-mail-exclusions-queries-and-counts).
- `count_mail` is a new read-only tool with `search_mail`'s filters, `unread_only`,
  exclusions and `query`. It returns `{folder, count}` from `UID SEARCH` alone and
  never fetches headers or bodies. Bridge matches every condition there, including raw
  `From`, `To` and `Subject` text, so a count can be lower than `search_mail` finds for
  RFC 2047 encoded non-ASCII headers. Header and text conditions make Bridge read each
  message in the folder; no All Mail default window applies.
- **Sieve filter previews (backlog P-B).** `preview_sieve_filter(name, folder,
  conditions, match, mark_read)` validates one Proton Sieve filter for new mail (1 to
  10 `from`, `to` or `subject` conditions using `contains` or `is`, an existing real
  `Folders/` target by canonical name, optional mark read) and returns the exact
  script, its SHA-256, the apply backend status and a single-use token bound to that
  script. Scripts copy the spam guard, extensions and comparator of filters made in
  Proton's editor; values are escaped as RFC 5228 quoted strings, and a strict parser
  refuses any script that is not exactly its filter (CR, LF, controls, invisible
  characters and `${` are refused outright). Filters only file into the target and
  mark read only when asked; they never discard, reject, redirect or reply.
- `create_sieve_filter(token)` confirms the exact stored script in the client's form,
  rechecks the script, the folder and the existing filter names (filters are never
  replaced, edited or removed), journals an intent, claims the token and adds the
  filter through a pluggable apply backend, audited like every write tool.
  **Proton Mail Bridge offers no way to apply Sieve filters** (IMAP and SMTP only, no
  ManageSieve, Sieve or filter interface), so `create_sieve_filter` always refuses
  with "Sieve filters cannot be applied" before any form, filter journal record or
  mailbox access, and nothing changes. The only shipped backend is this refusing one,
  and no setting selects another. See [Sieve filters](docs/rules.md#sieve-filters).
- An interrupted `create_sieve_filter` returns `partial: true` with `created` and
  `attempted`: created (a later journal append failed), uncertain (`attempted`
  without `created`: check Proton's filter list) or never sent to the backend
  (`attempted: false`, no filter created). Its audited outcome counts the filter once,
  as `done`, `uncertain` or `remaining`.
- `preview_sieve_filter` and `preview_rules` accept only a JSON boolean for
  `mark_read`: the MCP boundary no longer turns `"yes"`, `"true"`, `1` or `0` into a
  boolean, so these refuse as invalid arguments.
- **`compose_reply` new-draft mode.** With no `message_id` or `draft_id` it opens the
  composer blank, or prefilled with optional `to`, `cc`, `subject` and `body`, reading
  no mail. Saving goes through `create_draft`, which needs a recipient; the tool never
  sends. Reply quotes now collapse each run of empty `>` lines into one.

### Security

- No token reaches the service log. The SDK logs what a malformed request or notification
  carried, so a broken client could put a whole preview token in the log, or half of one
  inside a validation error. Every log record now loses token-shaped text and any value
  echoed from a refused request, on every logger and handler.
- `read_thread`, `show_thread` and `read_attachment` text results start with a one-sentence
  `security_notice`. `read_attachment` also removes control characters from the text (tab
  and line ends stay) and collapses 3 or more newlines to two; `total_chars` and offsets
  count the cleaned text.
- Every mutation token is bound to its exact previewed action set. One
  `approval_hash` (action, targets and params, selector and message rows,
  unsubscribe targets, draft bytes and SMTP envelope) is stored with cleanup,
  rules, unsubscribe, folder creation and send tokens, recomputed on every token
  lookup, and returned by each preview as `action_hash`. Tokens stay single use
  and short lived; a changed payload, replay or expiry refuses.
- Redeeming a cleanup, rules, unsubscribe or folder token needs the user's
  confirmation in an MCP form elicitation that shows the stored approval
  (`PROTON_MCP_CONFIRM_MUTATIONS=1` by default, `PROTON_MCP_CONFIRM_TIMEOUT`
  120 seconds by default). A declined, cancelled, unanswered, failed or
  unsupported form retires the token. Continuations inherit the confirmation of
  their approval. The token store enforces this as well as the tools.
- Every write tool call (`create_draft`, `send_draft`, the cleanup tools,
  `apply_rules`, `unsubscribe`, `create_folder`, `create_sieve_filter`,
  `undo_action`) is audited in the existing action log: a started record and an
  outcome record with timestamp, tool, client (MCP `clientInfo` and transport),
  outcome, action hash, token ID (never the token) and counts. Records contain no
  bodies, subjects, full addresses or secrets. A write refuses if its started record
  cannot be written. Every top-level journal record now carries `client`. A folder
  that was created although a later journal append failed counts as `done` only,
  not also as `uncertain`.
- `list_actions` gains `action` and `client_name` filters and an `audit` list.
- Bounded approvals and folder creation stay inside the token flow: the approval
  hash covers `create_folder_if_missing` and the bound, a tampered, declined or
  unconfirmed token creates no folder, a move approval without creation never
  creates a folder that vanished after its preview, a lowered configured bound
  refuses an issued larger approval, and continuations only ever name the
  untouched remainder of the bound set.
- `preview_send`, `review_send` and `send_draft` refuse a C0 control or DEL in any
  top-level draft header, raw or inside an encoded word, apart from a tab and CRLF line
  folding. Only address, subject and identity headers were checked before. In a draft from
  another client, a control in another header could reach the wire as a `Cc:` line the
  preview never showed on CPython before 3.12.5, and Bridge delivers to the Cc headers it
  reads from the message. A header Python's generator refuses or fails on now gives a validation
  error as well, where the tool used to blame the Bridge configuration.

### Changed

- **The PyPI package is named `mcp-proton-mail`.** PyPI refuses `proton-mail-mcp` as too close to
  another project's name. Once it is published, `uvx mcp-proton-mail setup` and
  `pipx install mcp-proton-mail` install it, and `server.json` names it for the MCP Registry. The
  package installs the `proton-mail-mcp` command as before, and the same command as
  `mcp-proton-mail`, which `uvx mcp-proton-mail` runs. The repository, the `proton_mail_mcp` module,
  the services and every path keep their names. A wheel is now
  `mcp_proton_mail-<version>-py3-none-any.whl`, the name `setup --source` takes. See Upgrade notes.
- **The install docs no longer say the repository is private.** The README, `llms-install.md`,
  `llms.txt`, `docs/install.md` and `docs/windows.md` clone it with
  `git clone https://github.com/legalaspro/proton-mail-mcp.git`, which needs no GitHub account or
  `gh`. The "Not on PyPI yet" warning stays. `docs/ci.md` no longer names the dev repo or quotes
  its run IDs and commits.
- **No address is built into the code.** The own-address path used to accept one more
  recipient, fixed in code as a SHA-256 digest. That is gone. With
  `PROTON_MCP_SEND_SELF_NO_FORM=1`, the addresses that skip the form are now only those the
  server reads at run time: `PROTON_BRIDGE_USER`, the name it logs in to Bridge with, and
  `PROTON_MCP_SEND_FROM`. Any other recipient takes the form or, on a client without one,
  the typed OK. See Upgrade notes.
- **Clearer guidance for the model.** The server instructions now state the workflow across
  tools (reading, preview then redeem, what to do after a refusal), and name only the tools a
  configuration registers. Every tool argument has a description in its input schema. Long tool
  descriptions lead with what the tool does and fit in 2,048 characters, so a client that cuts
  longer text loses no rule. Wrong text is fixed: a `limit` above the approved total refuses and
  keeps the token, `unsubscribe` and `create_folder` report `partial`, and a failed send may have
  gone out. Validation and behavior do not change.
- **Shorter README and reorganized docs.** The README now covers what the server does, its
  safety model and how to install it. [docs/README.md](docs/README.md) maps every page by what
  you need: [Getting started](docs/getting-started.md) is new,
  [docs/tools.md](docs/tools.md) is generated from the registered tools with `make docs`, and
  [Results, refusals and limits](docs/results.md) and
  [How approvals work](docs/safety.md) are new reference pages. The reference text moved, with
  its section names, to [docs/search.md](docs/search.md), [docs/drafts.md](docs/drafts.md),
  [docs/sending.md](docs/sending.md), [docs/cleanup.md](docs/cleanup.md),
  [docs/rules.md](docs/rules.md), [docs/configuration.md](docs/configuration.md),
  [docs/install.md](docs/install.md), [docs/RECOVERY.md](docs/RECOVERY.md),
  [docs/search-index.md](docs/search-index.md) and [docs/development.md](docs/development.md).
  New pages: [docs/clients.md](docs/clients.md) and [docs/windows.md](docs/windows.md).
- **`AGENTS.md` and `llms.txt`.** [AGENTS.md](AGENTS.md) is the guide for coding agents and
  contributors, and [llms.txt](llms.txt) indexes the docs for AI assistants.
- **The own-address path covers all the user's own send-from addresses.** With
  `PROTON_MCP_SEND_SELF_NO_FORM=1`, a plain draft whose every To, Cc and Bcc address is the
  account's or on `PROTON_MCP_SEND_FROM`, ignoring case, now reports `approval: "token"` and
  sends without a form, also from an alias. An alias note to yourself used to need the form,
  which a client without form support refuses. Any other recipient, an attachment or a
  forward subject still needs the form, and nothing changes with the flag off.
- **Protected senders are opt-in.** The committed defaults list nothing, so with
  `PROTON_MCP_PROTECTED_CONFIG` unset no list refuses a sender in cleanup, unsubscribe,
  draft deletion, rules or the rules timer, including banks, authorities and consumer
  domains. Missing, unparsable or ambiguous sender headers still refuse. The file format is
  unchanged, but a file now gives only what it lists; one that relied on the committed lists
  must add them (see Upgrade notes). A missing or invalid file still refuses cleanup.
  `create_draft`, `preview_send` and `send_draft` never read this policy and still do not.
- **Simpler guarded send.** On the own-address path, a To with an encoded display name now refuses at preview, as it
  always did with the form. Extra or non-string `send_draft` arguments are refused
  by argument validation: they leave the token live and write no send-journal entry,
  and the audit record stays `invalid_arguments`. Draft recipient edits journal
  `draft_changed` instead of `recipients_changed`, and `token_approval_disabled` is
  gone. A send reads the draft 3 times on the form path and twice on the own-address
  path, instead of 4. A draft edited during SMTP setup now refuses just before DATA,
  after a login. Cancellation stops the send by DATA at the latest.
- **Audit cuts C09, C11 and C12:** removed `ui_render_probe` and its static view,
  `list_unsubscribe`, and `recent_new_mail` with its in-memory event feed. Check
  MCP Apps rendering with `show_thread`, discover unsubscribe methods with
  `preview_action(action="unsubscribe", ids=[message_id])`, and use
  `search_mail(since=...)` for recent mail. The optional IDLE watcher still updates
  the search index. The surface is now 31 tools by default, or 34 with sending
  enabled (29 / 31 with Apps disabled). C10 is explicitly excluded: both Sieve
  filter tools remain available with their existing behavior.

- **Breaking: near-twin tools are merged, and the old names are gone.** The surface is
  now 25 tools by default, or 28 with sending enabled (23 / 25 with Apps disabled).
  - `mark_read`, `mark_unread` → `set_read_state(state="read"|"unread", token, ...)`
  - `add_label`, `remove_label` → `label(op="add"|"remove", token, ...)`
  - `move_to_trash`, `archive`, `move_to_folder` →
    `move(target="Trash"|"Archive"|"Folders/<name>", token, ...)`
  - `reindex_preview`, `reindex_execute`, `reindex_cancel` →
    `reindex(op="preview"|"execute"|"cancel", token=None, run_id=None)`;
    `reindex_status` stays separate and read-only.

  Approvals are as strict as before. `preview_action` keeps its `action` values and a
  token still binds one exact action, target and message set. The merged tools add a
  stated selector that must match: a `target`, `state` or `op` the token does not pin
  refuses before any form and without consuming the token, and what runs is always the
  stored approval. The confirmation form, undo and the action log are unchanged, and
  audit records keep naming the exact action (`"tool": "archive"`), so logs read the
  same before and after. `list_actions(action=...)` still takes those exact names and
  now also the merged names, where `action="move"` finds all three kinds. Arguments that
  select no action are audited under the tool's own name as `invalid_arguments`. Because
  `reindex` also starts rebuilds, `reindex(op="preview")` is no longer read-only: each
  preview is audited, and hosts may ask before calling it. `review_send`, `preview_send`,
  `list_actions` and `undo_action` keep their own tools.

- **Search-index schema v2 (audit cuts C22 and C23).** The header index no longer
  requires FTS5 or stores body/snippet text. Pre-v2 caches are rebuilt from
  scratch: opening a cache with `user_version` other than `2`, or with an FTS5
  table, discards its database and its `-wal` and `-shm` files and creates a fresh
  v2 schema. This works without FTS5 support and logs one safe discard message.
  Old headers and coverage proofs are discarded and restored through automatic
  startup recovery, normal cache warming or the approved manual reindex workflow.
  Current v2 caches are kept. Text
  predicates remain Bridge-backed, and the 37-tool surface is unchanged.

- Removed the offline `proton-mail-mcp-rebuild-search-index` CLI. Use the existing
  `reindex_preview` / `reindex_execute` background rebuild workflow instead.

- **One search completeness signal (audit cut C24).** With `partial=false`,
  follow `next_cursor` until it is null to collect the complete answer for the
  requested folder, filters and dates within the search's UID snapshot.
  Explicitly malformed INTERNALDATE values are omitted from matches and totals.
  Every partial response includes `partial_reason`. Removed `search_index`,
  `search_index_details` and `retryable`, including header/body completeness,
  result-source, fallback, timeout and candidate diagnostics. `total_is_exact`
  remains independent and stays true when unresolved canonical handles omit
  rows from an exact total. `next_cursor`, `applied_since`, `timed_out` and
  `error` retain their roles. Index health remains available through the reindex
  tools and server log. Mail arriving during a request is out of scope and
  belongs to the next request.
- Complete All Mail coverage now requires the unique inventoried UID count to
  match the selected mailbox's reported message count, as well as known headers
  for every UID. An inconsistent inventory therefore keeps the default 30-day
  window rather than removing it.

- **Consistent action-log identifiers.** Single logged actions use `log_id`;
  batch cleanup and `apply_rules` results use only `log_ids`, always a list,
  including empty and one-element results. IDs remain 32-character lowercase
  hexadecimal strings. `list_actions` uses `log_id` per action and `log_ids` per
  run. Clients should iterate batch `log_ids`, or read `log_ids[0]` when exactly
  one chunk exists, instead of the former nullable singular alias.
  `undo_action(log_id)` input is unchanged; previews return no action-log ID.
  Unlogged folder-creation results omit `log_id` instead of returning `null`;
  undo's `unresolved` rows use `log_id` instead of `outcome_log_id`.
- `preview_rules` reports a saved rule with `name`, `conditions`, `match` and
  `mark_read` instead of `field` and `contains`, and the rules confirmation names them;
  packaged rules report as before. The rules digest that rule tokens bind now also
  covers the saved rules (it stays the packaged file's digest while none is saved), so
  saving a rule invalidates outstanding rule previews, and an invalid saved rules store
  refuses rule previews, applies and timer runs.
- **Batched STORE/MOVE (backlog P-D, G-B).** `move_to_trash`, `archive`,
  `move_to_folder`, `mark_read` and `mark_unread` send one `UID MOVE` or
  `UID STORE` per source folder and chunk of up to 200 UIDs (compressed UID sets),
  instead of one command per message. Per-message final checks, intents and
  outcomes are kept, and every UID is verified after the command (a moved UID
  must be gone from its source, whatever `COPYUID` says, and have a confirmed new
  copy in the destination; a `\Seen` change is read back). Chunks of these
  actions hold up to 200 messages (label actions keep 50) and are budgeted at
  2.0 seconds per command plus 0.05 seconds per further UID instead of 2.0 seconds
  per message, so a slow server no longer splits a 30- or 200-message batch with
  "Insufficient execute headroom for verification". `estimated_calls` reflects the
  new allowance. A batched command that the server refuses, or whose reply is
  lost, leaves all of its messages (at most 200) `uncertain` for inspection, where
  a per-message command left one.
- `undo_action` restores a large chunk message by message and may now stop with
  `partial: true` and "Undo time budget reached; call undo_action again to restore
  the remaining messages"; repeating the call restores the rest. An attempt keeps
  half of its call budget for restores: messages it could not look up in time are
  not claimed, stay retryable and are counted in `deferred`, so a slow server can
  no longer exhaust every attempt on lookups for a 200-message chunk. Each further
  restore starts only with room for the slowest restore so far in the call (at
  least two seconds) instead of a fixed two seconds, so slow restores are not
  started just before the deadline and left uncertain.
- `search_mail` reads `FLAGS` in the batched candidate fetch it already sends
  (`UID INTERNALDATE FLAGS`). Searches without the new options send the same
  `UID SEARCH` and page `FETCH` commands as before and return the same fields plus
  `seen`; their cursors keep their key, so outstanding cursors stay valid.
- **Canonical message handles (backlog P-A).** `search_mail`, `read_thread` and preview
  samples report one `(folder, uidvalidity, uid)` per message: its handle in the real
  folder (`INBOX`, `Archive`, `Sent`, `Drafts`, `Spam`, `Trash` or `Folders/...`). This is
  the handle that moves, read-state changes and folder rules bind in their confirmation
  tokens. Previously `read_thread` returned the All Mail handle (for example All Mail
  47175) while `search_mail` returned INBOX 20321 for the same message. See
  [Canonical message handles](docs/search.md#canonical-message-handles).
- A message read through a view (All Mail, Starred or a `Labels/` mailbox) keeps that
  view's handle only as `alias`. Canonical fields are `null` when no unique real-folder
  copy is known, including identical duplicates, mail without a real folder, or a
  deadline, mailbox error or incomplete metadata during the lookup (which also sets
  `partial=true`). A missing FETCH record, or a missing or malformed `ENVELOPE`,
  `INTERNALDATE` or `RFC822.SIZE` for the view handle or any candidate copy, never
  lets the remaining copies count as unique.
- A view handle resolves only if the view, All Mail and every real folder read still
  report the UIDVALIDITY, UIDNEXT and message count they were scanned with when the
  lookup reopens them. A message arriving or leaving during the lookup, or a mailbox
  without UIDNEXT, leaves the canonical fields `null` with `partial=true`. An identical
  message delivered to INBOX mid-lookup therefore never lends its handle to a view-only
  message.
- Without an All Mail view, view handles are never resolved (no lookup runs): a
  Starred or label message without a real folder cannot be told apart from an
  identical INBOX message.
- Search and thread messages now include `uidvalidity`. Preview `samples` and `blocked`
  entries (`preview_action`, including unsubscribe, and `preview_rules`)
  now include `uidvalidity` and `uid` beside `folder`, replacing the earlier rule that
  samples omit UIDs. A label or All Mail source handle is the `alias`; its canonical
  handle comes from the same lookup as `search_mail`, run after the approval checks,
  and never from discovered memberships that merely share its Message-ID. The lookup
  starts only if the preview has used at most 40% of its budget. If it is skipped or
  fails, the fields are `null` and the preview reports `partial=true` with its token
  unchanged. Samples classify real folders by the same LIST attributes as
  search and guarded moves, so a canonical-looking name marked `\All`, `\Flagged` or
  `\Noselect` (in any case) is a view everywhere.
- `search_mail` matches `folder` to its LIST name before searching. `INBOX` in any
  ASCII case (`inbox`, `Inbox`) is selected and reported as LIST spells it and keeps
  its real-folder handles; other names must match exactly.
- `search_mail` reads LIST for every search, not only without date bounds.
  Execution, undo and journal records, tokens and their bindings are unchanged.

### Fixed

- `send_draft`'s description and argument text now say that `draft_id` is required after
  `approval: "typed_ok"` and that a call without it uses up the token. `preview_send`'s
  `next_steps` say the same, and `review_send`'s and `forward_message`'s text now names
  `send_draft(token, draft_id)` instead of `send_draft(token)`. `unsubscribe`'s description states
  the rule for its `send_preview`, and the server instructions say to pass both on a typed OK. The
  rule itself is unchanged.
- A confirmation form that gets no answer in time, is not shown or fails now refuses with an
  error saying so and that no other kind of confirmation applies, for sends and mailbox
  changes. A form reply saying there is no form is journaled as `confirmation_unavailable`,
  any other error reply as `confirmation_error`.
- `read_attachment` now gives U+0000 between two letters or digits as `-`, because some PDF
  fonts map a hyphen or dash glyph to it and `REF-0001` read `REF0001`. Any other U+0000 is
  still removed. The character is inferred: `:` or `+` lost the same way also reads `-`.
- Attachment sizes now say which size they are. `list_attachments`, `forward_message`,
  `read_thread`, `show_thread` and a saved message's rows in `compose_reply` report the
  transfer-encoded byte count, which `size_note` states, while `preview_send` reports decoded
  bytes, so one 29,218-byte PDF in base64 lists as 39,984. The thread view and composer mark
  those sizes, as in `39 KB encoded`; a local file's row and the send review show decoded
  bytes with no mark, and a `compose_reply` row with encoded bytes has `size_encoded: true`.
- Guarded send refuses a `Cc` or `Bcc` folded right after its colon on every Python version,
  as it already did for `To`. Newer parsers strip that line break, so Python 3.14 read it as
  a plain header.
- A stored draft whose address breaks the standard library's parser, or whose header its
  generator refuses, now gets the usual validation refusal instead of `Draft preview failed;
  check Bridge configuration`. Such a change after a preview is journaled as `draft_changed`.
- `read_attachment` now gives four hyphen characters in PDF text as a plain `-`: U+00AD,
  U+2010, U+2011 and U+2212. U+00AD is the one readers do not show, so `REF-0001` read as
  `REF0001`; pypdf returns it for code 0xAD of WinAnsi, which a PDF paints as a hyphen, and
  for a ToUnicode entry or glyph name mapped to it. The other three were visible, but a
  search for `-` missed them. En, em and other dashes stay as they are, and so does TXT and
  CSV text. Control-character removal is unchanged too, so a character with no Unicode
  mapping in the PDF's font can still be lost: pypdf can return its font code as a control
  character, which that removal deletes, and `REF-0001` can still read `REF0001`.
- `read_thread` keeps its canonical handles when an unrelated folder's index is stale.
  Canonical lookup needed a current proof for All Mail and every real folder, so one
  stale folder sent the whole thread to Bridge, where it often ended at the 3-second
  cap with "Canonical lookup reached its time limit". It now needs All Mail and the
  folders holding a copy of the thread's messages. A message whose folder fails its
  proof gets a live scan of the failing folders only, and the rest of the thread keeps
  its handles if that scan runs out of time. No folder gives a handle from its index
  unless that index matches live STATUS. All Mail search pages use the same lookup. A
  stale All Mail, as after new mail with no search since, still uses the whole live
  lookup. Plain search pages now cache each message's fingerprint, so reading a
  just-listed message no longer rescans its folder.
- Searching a real folder no longer drops its index proof. Every complete `ALL`
  inventory cleared the folder's certification and nothing restored it, so each plain
  INBOX search left later reads with no proof. One inventory now restores it when
  every live UID has known headers, counting headers that the same search fetched, and
  later searches of an unchanged real folder skip `UID SEARCH ALL`. A folder with a
  header the index cannot store stays uncertified and keeps its live inventory.
- `preview_rules` on a mailbox where one strict review costs half the call or more
  now reviews a whole batch of one rule in one go, instead of approving one match per
  call. On such a mailbox each rule needs its own approval and `order` holds only
  within a rule. `apply_rules` no longer holds back time to verify rows again that it
  has just verified, so such a batch moves in one call.
  A message whose copies have inconsistent sender headers or ambiguous label
  memberships no longer refuses the whole call: it stays in INBOX and is reported in
  the same `excluded_*` fields as `preview_action`. A message that cannot be verified
  still refuses; on a whole-batch call, the clean rows before it are served first. A
  failed call keeps its cursor and saved work, so it can be retried. `apply_rules`
  stays strict.
- A draft can no longer be delivered more than once. A new `preview_send` retires
  earlier live tokens for that draft, and `send_draft` refuses with `already_sent`
  when the journal holds `sent` or `partial` for its exact bytes and envelope. It
  refuses with `possibly_sent` when the server may have accepted the message: DATA
  started with no explicit refusal (`uncertain: true` in the result and the
  `send_result` record), or a reservation has no result. Check Sent, then edit the
  draft or create a new one. Failures before DATA and explicit 4xx or 5xx replies
  to DATA can still retry with a fresh preview. Both refusals come before any form.
- A To header folded right after its colon is refused on every Python version.
  Python 3.14 used to accept it.
- `preview_send` with a draft ID without angle brackets now returns a working token.
  It used to fail every redemption with `draft_changed`, and its `action_hash` differed
  from the bracketed form.

- Rules previews hold a tiny batch once when finishing the raw audit consumed the
  call, then probe a full batch with a warm review on the next call. Rules apply
  reuses that audit after mailbox revalidation and refreshes matching rows, avoiding
  a whole-folder raw reread when SELECT supplies UIDNEXT. Approval checks are unchanged.

- Automatic recovery attempts missing or unstorable headers at most three times
  per UID, warms later folders and ends with a fixed error when headers remain
  incomplete. It fetches newest UIDs first, skips healthy caches at startup,
  stops permanent failures and bounds retries with backoff unaffected by tool
  calls. Real-folder warming reports an unknown ETA instead of zero.
- `compose_reply` preserves partial seed-lookup reasons and index status in a
  retryable error rather than reporting a message or draft as missing.
- `reindex_status` reports `state="rebuilding"` only during queued or running
  automatic recovery. Inactive incomplete coverage reports its actual state,
  and failed or cancelled manual runs retain their run and swap details. Progress
  includes `indexed_count`,
  `target`, `percent` and an observed-rate `eta_seconds`; `percent_done` remains
  available for compatibility. Automatic recovery also reports `source="auto"`. Serving
  `indexed_count`, inventory `max_uid` and `last_catchup_at` describe current
  recovery; `last_full_run` preserves manual rebuild history.
- `read_thread` uses a live Message-ID seed lookup with a shared 12-second limit
  when a large mailbox has no verified index hint, avoiding hundreds of UID-range
  searches during index recovery. Raw headers verify the result; a capped lookup
  returns an explicit partial response, and canonical lookup remains independent
  of index completeness.
- `preview_rules` keeps its place in an unfinished All Mail membership walk when
  mailbox growth between requests increases EXISTS and UIDNEXT by the same amount,
  including with background sync off.
  Partial replies show `all_mail_audit=X/Y rewinds=N` in the existing `reason`
  text, without adding reply fields. New mail arriving during a call is out of
  scope for that call and is picked up on the next request. Edits and expunges
  still re-enumerate, and hidden duplicate identities still refuse approval.
- The All Mail walk in `preview_rules` checkpoints loop progress once per call in
  `finally`, preserving completed batches on deadlines while retaining its initial,
  final and fallback checkpoints.
- `preview_rules` replaces per-ID All Mail HEADER searches with UIDNEXT-derived
  raw fallback FETCH ranges of at most 200 UIDs, checking the deadline before
  each batch and saving its progress for continuation. Completed rows survive
  mailbox growth between requests, and certified peers retain their warm identity
  index. Preview response fields and `apply_rules` membership checks are unchanged.
- `preview_rules` resumes completed All Mail UID and raw membership checks after
  deadlines, including calls without a cursor, while revalidating mailbox state
  and refreshing candidate fields before approval.
- `preview_rules` preserves completed physical copy checks across cursor calls,
  revalidating mailbox state and refreshing candidate headers so slow folder
  checks make progress without approving unverified messages.
- `read_thread(include_thread=false)` skips live canonical lookup, avoiding slow
  real-folder scans after the requested body is read. Canonical `folder`, `uid`
  and `uidvalidity` are now always null in this mode, including messages read
  directly from real folders. Messages retain their source handle as `alias`
  without setting `partial=true` solely for the skipped lookup; use
  `search_mail` for real-folder handles. With thread expansion enabled, the seed
  lookup and requested body keep the full thread budget. Expansion and remaining
  body fetches stop early to reserve up to 3 seconds of the call
  budget for canonical lookup. Reaching that boundary reports
  `partial_reason="thread expansion limited"`, possibly with `timed_out=true` and
  `error` if an in-flight operation times out. Canonical lookup runs after the
  bodies with a 3-second cap within the call deadline. Exhausting only that cap
  preserves the messages and bodies, leaves unresolved view messages' canonical
  fields null and returns `partial=true` with a `partial_reason` explaining the
  canonical lookup time limit, without `timed_out` or `error`. If both limits are
  reached, the reason combines them as `"thread expansion limited; Canonical
  lookup reached its time limit; some canonical handles are unavailable"`.
- `read_thread` answers when its budget ends, with the messages already read,
  `partial=true` and `timed_out=true`, even if a step has not returned, such as a
  relatives search that the IMAP command watchdog cannot interrupt. Such a step
  previously held the reply until it returned, past the budget and possibly past
  the client's own timeout.
- `preview_rules` secures a one-row review before growing its batch and keeps
  completed approval evidence and its cursor if a later review times out or
  detects changed mailbox membership. Only the singleton may spend the response
  reserve. Larger reviews use their own timing samples and remaining budget;
  repeated overruns reduce later batch sizes. Reusing each review's folder list
  and candidate fields already fetched in the call reduces repeated work.
  Single-row calls and partial replies report time by stage to help investigate
  slow previews. Live verification of the reported `limit=1` failure is pending
  a retest.

- `read_thread` verifies seed Message-ID hints from the local index with a UID
  FETCH. Only after every folder's hint misses does it fall back to bounded live
  Message-ID searches, skipping empty folders. Deadline
  interruptions still return collected messages as partial results.
- `preview_rules` sizes strict review batches to the remaining deadline and
  shrinks stalled reviews so cursor calls complete batches and advance. A new
  cursor chain calibrates with up to 4 matches after securing one; later batches
  are sized from fitted fixed and per-row review times, excluding one-time raw
  folder audits, and reach `limit` when the call budget allows. Previews without a
  completed review issue no approval token.

- Search continuations count only confirmed unresolved handles toward skipping;
  canonical lookup errors return `partial_reason="canonical_lookup_failed"` and
  retry without skipping rows. Rejected UIDs (including malformed dates) stay in
  cursors unless complete-page encoding overflows, and timeouts that cache new
  headers do not consume retries. With no criteria, searches in every folder
  except All Mail, labels included, warm header coverage during that call.
  Recovery hints name `reindex_preview`, explicit user approval, then
  `reindex_execute`.

- Reindex previews made during a rebuild return its running `run_id` without
  another inventory. Already-running execute responses include `state="running"`.
  Preview labels its duration with `eta_is_estimate=true`, and token validity is
  described independently of the preview inventory. Candidate creation no longer
  emits a misleading missing-index warning.

- Cleanup and `apply_rules` no longer lose their continuation when the preview
  lifetime expires during an approved batch. Their first successful token claim
  starts one fixed 30-minute resume window; execution responses and continuation
  metadata report that deadline as `expires_at`. Unstarted previews retain their
  configured lifetime, and later claims, continuations and aliases never renew
  the resume deadline. Continuations authorize only the durable, untouched,
  eligible remainder with the same action, account, folder and message identities;
  completed or uncertain rows cannot be retried, and completion retires every
  capability. `move_to_folder` keeps its lost-response aliases; other actions
  keep single-use continuation tokens.
- Execute headroom uses the remaining safe budget without the previous 80%
  elapsed-time gate or refusal of a smaller trailing chunk. Batches process as
  much verified work as fits and return a continuation for the rest. Preview's
  80% gate, execution deadlines, response and journal reserves, and the final
  frozen-chunk check remain unchanged.
- `create_draft` refuses recipients that the address parser cannot read (such as
  deeply nested comments or an empty local part) as invalid, instead of reporting
  a mailbox failure.

### Upgrade notes

- A service environment that setup made before the package was renamed keeps the old
  `proton-mail-mcp` record beside `mcp-proton-mail` after a rerun, and the service runs the new
  copy. Do not `pip uninstall proton-mail-mcp` in it: both records list the same files. To start
  clean, move the environment away and rerun setup, which makes a new one.
- With `PROTON_MCP_SEND_SELF_NO_FORM=1`, an address of yours that should keep skipping the
  form must be `PROTON_BRIDGE_USER` or on `PROTON_MCP_SEND_FROM`. Any other address now takes
  the form, or the typed OK on a client without a form.
- With `PROTON_MCP_ENABLE_SEND=1`, a client whose `initialize` omits elicitation used to be
  refused and can now send to any recipient on a typed OK. To keep the refusal, set
  `PROTON_MCP_SEND_TYPED_OK=0` before deploying. Any value other than `0` or `1` stops startup.
- With `PROTON_MCP_SEND_SELF_NO_FORM=1`, every address on `PROTON_MCP_SEND_FROM` can now
  receive mail without a form. Before deploying, check that the list holds only your own
  addresses. With sending enabled, startup and the installer warn while the flag is on.
- The committed protected-sender defaults no longer list anything: no institutions, no
  approved personal-provider sender, and no generic patterns, keywords or consumer domains.
  Before upgrading, a deployment that relied on them must copy its current
  `protected_default.json`, from the installed package or git history, to a
  private local file. A deployment that already names a local policy adds the old lists
  to that file instead and keeps its path: the variable names one file, so pointing it at
  the copy drops the existing entries. Name that file in `PROTON_MCP_PROTECTED_CONFIG` for every process
  that reads mail: the service's settings file or plist, and `rules.env` if the rules
  timer is on. Without that, previews for any sender with usable headers issue tokens for
  trash, archive, move, label and read-state changes, draft deletion and unsubscribe, and
  the timer can move its mail. A local policy that lists consumer domains but not the
  previously approved sender refuses that sender, including undo of earlier moves.

- `PMM_SEND_TOKEN_APPROVAL` is renamed `PROTON_MCP_SEND_SELF_NO_FORM`. The old name is
  ignored, so a deployment still setting it returns to the host form until it sets the
  new name. Set `PROTON_MCP_SEND_SELF_NO_FORM=1` before deploying if the deployment uses
  the no-form path; startup and the installer warn while the old name is set. The new
  flag accepts only `0` or `1`; any other value now stops startup.

- Search clients must replace reads of `search_index`, `search_index_details`
  and `retryable` with `partial` / `partial_reason` for completeness and
  `next_cursor` for continuation. Match `canonical_unresolved` instead of
  `canonical_lookup_failed` in `partial_reason`. Refresh cached MCP tool
  descriptions after upgrading.

- The merged tools replace ten old tool names with no aliases: see "near-twin tools"
  under Changed. Update prompts and scripts that name them, and refresh cached tool
  lists. Host allow or deny lists keyed by tool name need review: a host can no longer
  allow `archive` without `move_to_trash`, `mark_read` without `mark_unread`, or
  `reindex(op="preview")` without `op="execute"` and `op="cancel"`, by name alone.
  `reindex` has no confirmation form, so keep it on "ask" if a person must approve
  rebuilds. For the other merged tools, the per-change confirmation form still shows the
  exact stored change, unless the operator disabled it.

- `count_mail` is a new tool and `search_mail` has new parameters. MCP hosts that
  cache tool lists need a fresh connector entry (or a reloaded tool list) to see them.
  The same applies to `preview_add_rule`, `add_rule` and `list_rules`.
- Run the server and the rules timer with the same `PROTON_MCP_ACTION_LOG` (or
  `PROTON_MCP_SAVED_RULES`) so the timer reads the rules `add_rule` saves. Earlier
  releases ignore the saved rules store.
- With confirmation on (the default), the server uses stateful Streamable HTTP
  sessions with SSE, as it already did with guarded send enabled. Clients must
  initialize a session.
- Hosts without MCP form elicitation can no longer redeem mutation tokens unless
  the operator sets `PROTON_MCP_CONFIRM_MUTATIONS=0`. Verify form support before
  upgrading an existing install, or set the opt-out deliberately.
- The action log also holds the audit trail and is never rotated automatically;
  see [Location and rotation](docs/RECOVERY.md#location-and-rotation).
