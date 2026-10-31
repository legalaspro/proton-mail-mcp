# Contributing

## Mail arriving during a request

New mail arriving during a request (mid-call) is out of scope for that call and
is picked up on the next request. Do not implement, test, review, or require
handling for mid-call arrivals. Agents using the MCP tools should request mail
again when they need newer mail.
