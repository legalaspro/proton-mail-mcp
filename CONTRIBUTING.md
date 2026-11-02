# Contributing

[AGENTS.md](AGENTS.md) has the commands, layout, conventions and safety rules, for people and
coding agents alike. Tests and offline checks are described in
[docs/development.md](docs/development.md).

## Before you start

- Open an issue before anything larger than a small fix, so we can agree on the change first.
- Open one, too, before anything AGENTS.md lists under
  [Ask first](AGENTS.md#safety-rules): a change that widens a gate or loosens a default, a new
  dependency, a new tool, or anything that touches a real mailbox, a live Bridge or a real
  credential store.
- Report a security problem privately, as [SECURITY.md](SECURITY.md) says, never in an issue or a
  pull request.

## Making the change

- Set up as AGENTS.md shows, then run `make check`. It must pass.
- Run `make docs` after changing a tool's docstring, its arguments or the server instructions.
- Add an entry to [CHANGELOG.md](CHANGELOG.md) under Unreleased for a change in behaviour.
- Test data uses reserved names such as `example.test`. Never put real mail, addresses, tokens,
  passwords or logs in code, tests, issues or pull requests.

## The pull request

- Say what changed, how it was checked, and what was not run.
- Pull requests are squash-merged, so each becomes one commit on `main`.
- This repository is published from a private development tree. An accepted change is applied
  there too, with you credited as its author, so later updates keep it.

## License

Contributions are under the [Apache-2.0 license](LICENSE), as section 5 of the license says: what
you submit is licensed on the same terms as the project. There is no CLA or DCO to sign.

## Scope

New mail arriving during a request (mid-call) is out of scope for that call and is picked up on
the next request. Do not implement, test, review, or require handling for mid-call arrivals.
