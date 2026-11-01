# Contributing

[AGENTS.md](AGENTS.md) has the commands, layout, conventions and safety rules, for people and
coding agents alike. Tests and offline checks are described in
[docs/development.md](docs/development.md).

## Mail arriving during a request

New mail arriving during a request (mid-call) is out of scope for that call and is picked up on
the next request. Do not implement, test, review, or require handling for mid-call arrivals.
