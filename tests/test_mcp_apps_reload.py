"""Views whose host leaves out the result's data read it again through their own tool.

Claude Desktop passes a view only a tool result's content and isError: no _meta and no
structuredContent. Without the data a view would wait forever, so it calls its own view
tool once more with the arguments the host delivered in tool-input.
"""

import pytest
from test_mcp_apps_reply import reply
from test_mcp_apps_review_send import review
from test_mcp_apps_thread import _thread
from view_browser import HOST_CAPABILITIES, browser, requests, run_view, tool_result

from proton_mail_mcp import mcp_apps, views

ARGUMENTS = {
    "thread": {"message_id": "<m1@example.test>"},
    "reply": {"message_id": "<parent@example.test>"},
    "review_send": {"draft_id": "<review-1@example.test>"},
}
# The element each view fills from its data, and the text it shows once loaded.
LOADED = {
    "thread": ("subject", "Thread"),
    "reply": ("heading", "Re: Quarterly plan"),
    "review_send": ("subject", "Quarterly plan"),
}
# The element where each view says it could not load.
FAILED = {
    "thread": ("subject", "The thread could not be loaded"),
    "reply": ("heading", "The composer could not be loaded"),
    "review_send": ("subject", "The send review could not be loaded"),
}


def full(name):
    """The whole result, as the server returns it and as ChatGPT delivers it."""
    if name == "thread":
        return tool_result(mcp_apps.view_result(*views.thread_view(_thread(2), "m1@example.test")))
    if name == "reply":
        return tool_result(mcp_apps.view_result(*reply()))
    return review()


def content_only(result):
    """What Claude Desktop forwards to a view: only content and isError."""
    return {"content": result["content"], "isError": result.get("isError", False)}


def scenario(name):
    loaded_id, loaded_text = LOADED[name]
    failed_id, failed_text = FAILED[name]
    return f"""
const doc = t.doc;
const text = (id) => doc.getElementById(id).textContent;
const outcome = await t.waitFor(
  () =>
    text("{loaded_id}") === "{loaded_text}"
      ? "loaded"
      : text("{failed_id}") === "{failed_text}"
        ? "failed"
        : null,
  "the view to load or fail"
);
t.check("outcome", outcome);
if (config.again) {{
  // A host that delivers the same result again starts no second read.
  send({{ method: "ui/notifications/tool-result", params: config.result }});
  await t.sleep(200);
}}
if (outcome === "failed") {{
  const failure = doc.querySelector(".load-failure");
  t.check("failure", failure ? failure.firstChild.textContent : null);
  const retry = failure && Array.from(failure.querySelectorAll("button")).find(
    (button) => button.textContent === "Retry" && t.visible(button)
  );
  t.check("retry", !!retry);
  if (retry && config.retry) {{
    retry.click();
    t.check("retrying", retry.disabled);
    t.check(
      "after_retry",
      await t.waitFor(
        () =>
          text("{loaded_id}") === "{loaded_text}"
            ? "loaded"
            : doc.querySelector(".load-failure button:not([disabled])")
              ? "failed"
              : null,
        "the retry"
      )
    );
  }}
}}
t.check("loading_left", text("{loaded_id}").includes("…"));
"""


def run(tmp_path, name, result, *, tools=None, capabilities=None, again=False, retry=False, wait=0):
    page_scenario = scenario(name)
    if again or retry:
        flags = f"config.again = {str(again).lower()}; config.retry = {str(retry).lower()};\n"
        page_scenario = flags + page_scenario
    if wait:
        # Chrome runs on virtual time, so a long wait costs no real time.
        page_scenario = f"await t.sleep({wait});\n" + page_scenario
    report = run_view(
        tmp_path,
        name,
        result,
        page_scenario,
        arguments=ARGUMENTS[name],
        tools=tools,
        capabilities=capabilities,
    )
    return report["checks"], requests(report, "tools/call")


@browser
@pytest.mark.parametrize("name", list(mcp_apps.VIEWS))
def test_a_result_with_its_meta_renders_without_calling_a_tool(tmp_path, name):
    checks, calls = run(tmp_path, name, full(name))
    assert checks["outcome"] == "loaded" and checks["loading_left"] is False
    assert calls == []


@browser
@pytest.mark.parametrize("name", list(mcp_apps.VIEWS))
def test_view_data_passed_on_as_structured_content_renders_without_calling_a_tool(tmp_path, name):
    result = full(name)
    data = result["_meta"][mcp_apps.VIEW_META_KEY]
    checks, calls = run(tmp_path, name, {**content_only(result), "structuredContent": data})
    assert checks["outcome"] == "loaded"
    assert calls == []


@browser
@pytest.mark.parametrize("name", list(mcp_apps.VIEWS))
def test_structured_content_for_another_view_is_read_again(tmp_path, name):
    tool = mcp_apps.VIEW_TOOLS[name]
    result = full(name)
    other = {**content_only(result), "structuredContent": {"view": "another"}}
    checks, calls = run(tmp_path, name, other, tools={tool: result})
    assert checks["outcome"] == "loaded"
    assert [call["params"]["name"] for call in calls] == [tool]


@browser
@pytest.mark.parametrize("name", list(mcp_apps.VIEWS))
def test_a_content_only_result_is_read_again_once_through_the_views_own_tool(tmp_path, name):
    tool = mcp_apps.VIEW_TOOLS[name]
    result = full(name)
    checks, calls = run(tmp_path, name, content_only(result), tools={tool: result}, again=True)
    assert checks["outcome"] == "loaded" and checks["loading_left"] is False
    # Exactly one read, of the view's own tool with the host's arguments, however often the
    # host delivers the result.
    assert [call["params"] for call in calls] == [{"name": tool, "arguments": ARGUMENTS[name]}]


@browser
@pytest.mark.parametrize("name", list(mcp_apps.VIEWS))
def test_a_failed_read_says_why_and_retries_only_when_asked(tmp_path, name):
    tool = mcp_apps.VIEW_TOOLS[name]
    result = full(name)
    error = {"isError": True, "content": [{"type": "text", "text": "Bridge is not reachable"}]}
    checks, calls = run(
        tmp_path, name, content_only(result), tools={tool: [error, result]}, again=True, retry=True
    )
    # No endless loading: the reason and a Retry button, and no second read until it is pressed.
    assert checks["outcome"] == "failed"
    assert checks["failure"] == "Bridge is not reachable"
    assert checks["retry"] is True and checks["retrying"] is True
    assert checks["after_retry"] == "loaded"
    assert [call["params"]["name"] for call in calls] == [tool, tool]


@browser
@pytest.mark.parametrize("name", list(mcp_apps.VIEWS))
def test_a_host_that_strips_every_result_gets_a_clear_message(tmp_path, name):
    tool = mcp_apps.VIEW_TOOLS[name]
    stripped = content_only(full(name))
    checks, calls = run(tmp_path, name, stripped, tools={tool: [stripped, stripped]}, retry=True)
    assert checks["outcome"] == "failed"
    assert "did not pass the view its data" in checks["failure"]
    assert "still has the text result" in checks["failure"]
    assert checks["after_retry"] == "failed" and len(calls) == 2


# A host that does not declare serverTools: the view still tries its one read.
UNDECLARED = {key: value for key, value in HOST_CAPABILITIES.items() if key != "serverTools"}


@browser
@pytest.mark.parametrize("name", list(mcp_apps.VIEWS))
def test_a_host_that_does_not_declare_tool_calls_is_still_read_again_once(tmp_path, name):
    tool = mcp_apps.VIEW_TOOLS[name]
    result = full(name)
    checks, calls = run(
        tmp_path, name, content_only(result), tools={tool: result}, capabilities=UNDECLARED
    )
    assert checks["outcome"] == "loaded" and checks["loading_left"] is False
    assert [call["params"] for call in calls] == [{"name": tool, "arguments": ARGUMENTS[name]}]


@browser
@pytest.mark.parametrize("name", list(mcp_apps.VIEWS))
def test_a_host_that_refuses_tool_calls_is_told_apart(tmp_path, name):
    tool = mcp_apps.VIEW_TOOLS[name]
    refused = {"error": {"code": -32601, "message": "Method not found"}}
    checks, calls = run(
        tmp_path, name, content_only(full(name)), tools={tool: refused}, capabilities=UNDECLARED
    )
    assert checks["outcome"] == "failed" and checks["retry"] is True
    assert "does not let views call tools" in checks["failure"]
    assert "still has the text result" in checks["failure"]
    assert len(calls) == 1


@browser
@pytest.mark.parametrize("name", list(mcp_apps.VIEWS))
def test_a_host_that_never_answers_an_undeclared_tool_call_is_told_apart(tmp_path, name):
    tool = mcp_apps.VIEW_TOOLS[name]
    silent = {"unanswered": True}
    checks, calls = run(
        tmp_path,
        name,
        content_only(full(name)),
        tools={tool: silent},
        capabilities=UNDECLARED,
        wait=31000,
    )
    # Not 150 seconds of loading: a host that did not declare serverTools gets 30.
    assert checks["outcome"] == "failed" and checks["retry"] is True
    assert "does not let views call tools" in checks["failure"]
    assert len(calls) == 1
