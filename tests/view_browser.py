"""Run an MCP Apps view in headless Chrome against a scripted host.

The host page loads the view's real HTML in a sandboxed srcdoc frame, answers
ui/initialize, delivers one tool result and then runs a JavaScript scenario
that drives the view and records what it observed. Tests using it are skipped
when no Chrome or Chromium binary is installed. Nothing is loaded from the
network: the page, the view and its data are local.
"""

import html
import json
import os
import re
import shutil
import subprocess

import pytest

from proton_mail_mcp import mcp_apps

_BROWSERS = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
CHROME = next(filter(None, map(shutil.which, _BROWSERS)), None)
browser = pytest.mark.skipif(CHROME is None, reason="needs headless Chrome or Chromium")

HOST_CAPABILITIES = {
    "openLinks": {},
    "serverTools": {},
    "message": {"text": {}},
    "updateModelContext": {"text": {}},
}

HOST_CONTEXT = {"theme": "light", "displayMode": "inline", "locale": "en-GB", "timeZone": "UTC"}

_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>view test host</title></head>
<body>
<pre id="report"></pre>
<script type="application/json" id="config">@CONFIG@</script>
<script>
"use strict";
const config = JSON.parse(document.getElementById("config").textContent);
const log = [];
const checks = {};
const held = [];
const frame = document.createElement("iframe");
frame.setAttribute("sandbox", "allow-scripts allow-same-origin");
frame.style.width = "760px";
frame.style.height = "1200px";
frame.srcdoc = config.view;
document.body.appendChild(frame);

function send(message) {
  frame.contentWindow.postMessage(Object.assign({ jsonrpc: "2.0" }, message), "*");
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function answer(call) {
  const queued = config.tools[call.params.name];
  const result = Array.isArray(queued) ? queued.shift() : queued;
  if (result && result.unanswered) return;
  if (result && result.error) {
    // A JSON-RPC error instead of a tool result, such as a host that refuses tools/call.
    send({ id: call.id, error: result.error });
    return;
  }
  send({
    id: call.id,
    result: result || { isError: true, content: [{ type: "text", text: "unexpected tool call" }] },
  });
}

// The scenario's view of the host.
const t = {
  get doc() {
    return frame.contentDocument;
  },
  log,
  held,
  hold: false,
  requests(method) {
    return log.filter((message) => message.method === method);
  },
  async waitFor(condition, label) {
    for (let attempt = 0; attempt < 500; attempt++) {
      let value = null;
      try {
        value = condition();
      } catch (error) {
        value = null;
      }
      if (value) return value;
      await sleep(20);
    }
    throw new Error("timed out waiting for " + label);
  },
  check(name, value) {
    checks[name] = value;
  },
  key(node, key, options) {
    const init = Object.assign({ key: key, bubbles: true, cancelable: true }, options || {});
    return node.dispatchEvent(new KeyboardEvent("keydown", init));
  },
  input(node, value) {
    node.value = value;
    node.dispatchEvent(new Event("input", { bubbles: true }));
  },
  release() {
    const call = held.shift();
    answer(call);
    return call;
  },
  visible(node) {
    return !!node && !node.closest(".hidden") && node.getClientRects().length > 0;
  },
  button(label) {
    return Array.from(t.doc.querySelectorAll("button")).find(
      (button) => button.textContent === label && t.visible(button)
    );
  },
  sleep: sleep,
};

window.addEventListener("message", (event) => {
  if (event.source !== frame.contentWindow) return;
  const message = event.data;
  log.push(message);
  if (message.method === "ui/initialize") {
    send({
      id: message.id,
      result: {
        protocolVersion: "2026-01-26",
        hostInfo: { name: "test-host", version: "1" },
        hostCapabilities: config.capabilities,
        hostContext: config.hostContext,
      },
    });
  } else if (message.method === "ui/notifications/initialized") {
    send({ method: "ui/notifications/tool-input", params: { arguments: config.arguments } });
    send({ method: "ui/notifications/tool-result", params: config.result });
  } else if (message.method === "tools/call") {
    if (t.hold) held.push(message);
    else answer(message);
  } else if (message.id !== undefined && typeof message.method === "string") {
    if (config.refuse.includes(message.method)) {
      send({ id: message.id, error: { code: -32000, message: "Link opening denied by user" } });
    } else {
      send({ id: message.id, result: {} });
    }
  }
});

async function scenario(t) {
@SCENARIO@
}

(async () => {
  let report;
  try {
    await t.waitFor(
      () => log.some((message) => message.method === "ui/notifications/initialized"),
      "the view to connect"
    );
    await sleep(50);
    await scenario(t);
    report = { ok: true, checks: checks, log: log };
  } catch (error) {
    const reason = String((error && error.stack) || error);
    report = { ok: false, error: reason, checks: checks, log: log };
  }
  document.getElementById("report").textContent = JSON.stringify(report);
})();
</script>
</body></html>
"""


def tool_result(result):
    """A CallToolResult as the host forwards it to the view."""
    return result.model_dump(mode="json", by_alias=True, exclude_none=True)


def run_view(
    tmp_path,
    name,
    result,
    scenario,
    *,
    arguments=None,
    tools=None,
    capabilities=None,
    refuse=(),
    host_context=None,
    browser_tz=None,
):
    """Run scenario (the body of an async JavaScript function of t) and return its report.

    tools maps tool names to results (a list is answered in order; {"error": ...} is
    answered as a JSON-RPC error and {"unanswered": True} not at all); refuse lists
    host request methods that the host answers with an error. host_context replaces the
    host context's keys (None drops one), and browser_tz is the browser's own TZ.
    """
    context = {**HOST_CONTEXT, **(host_context or {})}
    config = {
        "view": mcp_apps.view_html(name),
        "result": result,
        "arguments": arguments or {},
        "tools": tools or {},
        "capabilities": HOST_CAPABILITIES if capabilities is None else capabilities,
        "refuse": list(refuse),
        "hostContext": {key: value for key, value in context.items() if value is not None},
    }
    head, tail = _PAGE.split("@SCENARIO@")
    page = head.replace("@CONFIG@", json.dumps(config).replace("<", "\\u003c")) + scenario + tail
    path = tmp_path / "host.html"
    path.write_text(page, encoding="utf-8")
    command = [
        CHROME,
        "--headless=new",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-gpu",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-default-apps",
        "--disable-sync",
        "--no-pings",
        "--no-proxy-server",
        # Nothing can resolve, so nothing can be fetched; the page is a local file.
        "--host-resolver-rules=MAP * ~NOTFOUND",
        "--mute-audio",
        f"--user-data-dir={tmp_path / 'chrome-profile'}",
        "--window-size=900,1400",
        # Timers run on virtual time, so scenarios do not depend on machine load.
        "--virtual-time-budget=60000",
        "--dump-dom",
        path.as_uri(),
    ]
    # Popen because conftest forbids general subprocess.run network helpers. The
    # browser gets a private home and no inherited environment.
    environment = {"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    if browser_tz is not None:
        environment["TZ"] = browser_tz
    with subprocess.Popen(
        command,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    ) as process:
        try:
            stdout, _ = process.communicate(timeout=180)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            pytest.fail("Headless Chrome did not finish the view scenario", pytrace=False)
    match = re.search(r'<pre id="report">(.*?)</pre>', stdout, re.S)
    assert match and match.group(1), f"no scenario report (Chrome exit {process.returncode})"
    report = json.loads(html.unescape(match.group(1)))
    assert report["ok"], report["error"]
    return report


def requests(report, method):
    return [message for message in report["log"] if message.get("method") == method]
