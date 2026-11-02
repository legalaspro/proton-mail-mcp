/* Proton Mail MCP view bridge: MCP Apps @PROTOCOL_VERSION@ JSON-RPC 2.0 over postMessage.
   Views never insert mail-derived text as markup: they build nodes with textContent.
   Only the parent window (the host) is trusted as a message source. */
"use strict";
var PMM = (function () {
  var PROTOCOL_VERSION = "@PROTOCOL_VERSION@";
  // The result _meta key with a view's data; hosts keep _meta out of model context.
  var VIEW_KEY = "@VIEW_META_KEY@";
  // The tool whose result this view shows: the only tool it may call to read its data again.
  var VIEW_TOOL = "@VIEW_TOOL@";
  // The view field of this view's data.
  var VIEW_NAME = "@VIEW_NAME@";
  var host = window.parent;
  var nextId = 1;
  var pending = new Map();
  var handlers = {};
  // Callbacks waiting for the host's tool-input, and the arguments each re-read used.
  var inputWaiters = [];
  var generation = 0;
  // The current re-read { promise, settled, generation }, and the data it returned.
  var reread = null;
  var reloaded = null;
  var state = {
    connected: false,
    failed: false,
    protocolVersion: null,
    hostInfo: null,
    hostCapabilities: {},
    hostContext: {},
    toolInput: null,
    toolResult: null,
  };

  function post(message) {
    if (host && host !== window) {
      host.postMessage(message, "*");
    }
  }

  function emit(name, value) {
    var callback = handlers[name];
    if (typeof callback === "function") {
      try {
        callback(value);
      } catch (error) {
        report("view handler failed: " + name);
      }
    }
  }

  function request(method, params, timeoutMs) {
    var id = nextId++;
    return new Promise(function (resolve, reject) {
      var timer = setTimeout(function () {
        pending.delete(id);
        var failure = new Error("The host did not answer " + method);
        failure.timedOut = true;
        reject(failure);
      }, timeoutMs || 30000);
      pending.set(id, { resolve: resolve, reject: reject, timer: timer });
      post({ jsonrpc: "2.0", id: id, method: method, params: params || {} });
    });
  }

  function notify(method, params) {
    post({ jsonrpc: "2.0", method: method, params: params || {} });
  }

  function respond(id, result) {
    post({ jsonrpc: "2.0", id: id, result: result });
  }

  function report(text) {
    notify("notifications/message", { level: "warning", logger: "proton-mail-view", data: text });
  }

  function isObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function applyContext(context) {
    var root = document.documentElement;
    if (context.theme === "light" || context.theme === "dark") {
      root.setAttribute("data-theme", context.theme);
    }
    if (typeof context.displayMode === "string") {
      root.setAttribute("data-display-mode", context.displayMode);
    }
    var styles = isObject(context.styles) ? context.styles : {};
    var variables = isObject(styles.variables) ? styles.variables : {};
    Object.keys(variables).forEach(function (name) {
      var value = variables[name];
      // Host theme tokens only: plain custom-property values, no blocks or markup.
      if (
        /^--[A-Za-z0-9-]{1,64}$/.test(name) &&
        typeof value === "string" &&
        value.length <= 200 &&
        !/[;{}<>\\]|url\(/i.test(value)
      ) {
        root.style.setProperty(name, value);
      }
    });
  }

  function mergeContext(update) {
    if (!isObject(update)) return;
    Object.keys(update).forEach(function (key) {
      state.hostContext[key] = update[key];
    });
    applyContext(state.hostContext);
    emit("context", state.hostContext);
  }

  window.addEventListener("message", function (event) {
    if (event.source !== host) return;
    var message = event.data;
    if (!isObject(message) || message.jsonrpc !== "2.0") return;
    if (typeof message.method !== "string") {
      var entry = pending.get(message.id);
      if (!entry) return;
      pending.delete(message.id);
      clearTimeout(entry.timer);
      if (isObject(message.error)) {
        var failure = new Error(
          typeof message.error.message === "string" ? message.error.message : "Host error"
        );
        failure.code = message.error.code;
        entry.reject(failure);
      } else {
        entry.resolve(message.result);
      }
      return;
    }
    var params = isObject(message.params) ? message.params : {};
    if (message.id !== undefined && message.id !== null) {
      if (message.method === "ui/resource-teardown") {
        emit("teardown", params);
        respond(message.id, {});
      } else if (message.method === "ping") {
        respond(message.id, {});
      } else {
        post({
          jsonrpc: "2.0",
          id: message.id,
          error: { code: -32601, message: "Method not found" },
        });
      }
      return;
    }
    if (message.method === "ui/notifications/tool-input") {
      var input = isObject(params.arguments) ? params.arguments : {};
      if (state.toolInput !== null && JSON.stringify(input) !== JSON.stringify(state.toolInput)) {
        // Other arguments are another call: nothing read for the last one applies.
        generation += 1;
        reread = null;
        reloaded = null;
      }
      state.toolInput = input;
      inputWaiters.splice(0).forEach(function (wake) {
        wake();
      });
      emit("toolInput", state.toolInput);
    } else if (message.method === "ui/notifications/tool-result") {
      state.toolResult = params;
      emit("toolResult", params);
    } else if (message.method === "ui/notifications/tool-cancelled") {
      emit("toolCancelled", params);
    } else if (message.method === "ui/notifications/host-context-changed") {
      mergeContext(params);
    }
  });

  function watchSize() {
    var root = document.getElementById("app") || document.body;
    var last = 0;
    var scheduled = false;
    function send() {
      scheduled = false;
      var height = Math.ceil(root.getBoundingClientRect().height);
      if (height > 0 && height !== last) {
        last = height;
        notify("ui/notifications/size-changed", { height: height });
      }
    }
    function schedule() {
      // A timer, not requestAnimationFrame: hosts may throttle frames offscreen.
      if (!scheduled) {
        scheduled = true;
        setTimeout(send, 16);
      }
    }
    if (typeof ResizeObserver === "function") {
      new ResizeObserver(schedule).observe(root);
    }
    // Resize observations wait for rendering; DOM changes alone also trigger a check.
    if (typeof MutationObserver === "function") {
      new MutationObserver(schedule).observe(root, {
        childList: true,
        subtree: true,
        attributes: true,
        characterData: true,
      });
    }
    window.addEventListener("resize", schedule);
    schedule();
  }

  function connect(appInfo, options) {
    handlers = Object.assign(handlers, (options && options.on) || {});
    return request(
      "ui/initialize",
      {
        protocolVersion: PROTOCOL_VERSION,
        appInfo: appInfo,
        appCapabilities: {
          availableDisplayModes: (options && options.displayModes) || ["inline"],
        },
      },
      10000
    ).then(
      function (result) {
        result = isObject(result) ? result : {};
        state.connected = true;
        state.protocolVersion =
          typeof result.protocolVersion === "string" ? result.protocolVersion : null;
        state.hostInfo = isObject(result.hostInfo) ? result.hostInfo : null;
        state.hostCapabilities = isObject(result.hostCapabilities) ? result.hostCapabilities : {};
        notify("ui/notifications/initialized", {});
        mergeContext(isObject(result.hostContext) ? result.hostContext : {});
        // Size reports start once the handshake is complete.
        watchSize();
        return state;
      },
      function (error) {
        state.failed = true;
        throw error;
      }
    );
  }

  function toolResultError(result) {
    var content = isObject(result) && Array.isArray(result.content) ? result.content : [];
    var text = content
      .filter(function (block) {
        return isObject(block) && block.type === "text" && typeof block.text === "string";
      })
      .map(function (block) {
        return block.text;
      })
      .join("\n");
    return text || "The tool call failed";
  }

  function callTool(name, args, timeoutMs) {
    return request("tools/call", { name: name, arguments: args || {} }, timeoutMs || 150000).then(
      function (result) {
        if (!isObject(result) || result.isError) {
          throw new Error(toolResultError(result));
        }
        return result;
      }
    );
  }

  function can(capability) {
    return isObject(state.hostCapabilities) && state.hostCapabilities[capability] !== undefined;
  }

  function sendMessage(text) {
    return request(
      "ui/message",
      { role: "user", content: [{ type: "text", text: text }] },
      30000
    ).then(function (result) {
      if (isObject(result) && result.isError) throw new Error("The host declined the message");
      return result;
    });
  }

  function updateModelContext(text, structured) {
    var params = { content: [{ type: "text", text: text }] };
    if (structured) params.structuredContent = structured;
    return request("ui/update-model-context", params, 30000);
  }

  function openLink(url) {
    return request("ui/open-link", { url: url }, 30000).then(function (result) {
      if (isObject(result) && result.isError) throw new Error("The host declined to open the link");
      return result;
    });
  }

  function requestDisplayMode(mode) {
    return request("ui/request-display-mode", { mode: mode }, 30000);
  }

  /* DOM helpers shared by views: text only, never markup. */
  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
    return node;
  }

  function safeUrl(value) {
    if (typeof value !== "string" || value.length > 4096) return null;
    var url;
    try {
      url = new URL(value);
    } catch (error) {
      return null;
    }
    return url.protocol === "https:" || url.protocol === "http:" || url.protocol === "mailto:"
      ? url.href
      : null;
  }

  // A Date header's time in the user's zone: the host's timeZone, else the browser's own.
  // Bridge writes Date headers in UTC, so the raw value would show UTC.
  function formatDate(value) {
    if (typeof value !== "string" || !value) return "";
    // A trailing comment such as "(UTC)" is not part of the date, and some engines refuse it.
    var parsed = Date.parse(value.replace(/\s*\([^()]*\)\s*$/, ""));
    if (isNaN(parsed)) return value;
    var context = state.hostContext || {};
    var options = { dateStyle: "medium", timeStyle: "short" };
    if (typeof context.timeZone === "string") options.timeZone = context.timeZone;
    try {
      return new Intl.DateTimeFormat(context.locale || undefined, options).format(parsed);
    } catch (error) {
      try {
        return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(
          parsed
        );
      } catch (ignored) {
        return value;
      }
    }
  }

  function formatSize(bytes) {
    if (typeof bytes !== "number" || !isFinite(bytes) || bytes < 0) return "";
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(bytes < 10240 ? 1 : 0) + " KB";
    return (bytes / (1024 * 1024)).toFixed(1) + " MB";
  }

  // size_encoded marks the transfer-encoded count stored in a message, larger than the file
  // for base64. Any other size is the file's decoded bytes.
  function fileDetails(item) {
    var size = formatSize(item.size);
    return [item.type, size && item.size_encoded ? size + " encoded" : size]
      .filter(Boolean)
      .join(" · ");
  }

  function splitAddress(value) {
    var text = typeof value === "string" ? value.trim() : "";
    var match = /^(.*?)\s*<([^<>]+)>\s*$/.exec(text);
    var name = match ? match[1].replace(/^"(.*)"$/, "$1").trim() : "";
    var address = match ? match[2].trim() : text;
    return { name: name || address, address: address };
  }

  function avatar(value) {
    var parts = splitAddress(value);
    var letters = parts.name
      .replace(/[^\p{L}\p{N} ]/gu, " ")
      .trim()
      .split(/\s+/)
      .filter(Boolean)
      .slice(0, 2)
      .map(function (word) {
        return Array.from(word)[0].toUpperCase();
      })
      .join("");
    var node = el("span", "avatar", letters || "?");
    var hash = 0;
    Array.from(parts.address.toLowerCase()).forEach(function (char) {
      hash = (hash * 31 + char.codePointAt(0)) % 360;
    });
    node.style.setProperty("--avatar-hue", String(hash));
    node.setAttribute("aria-hidden", "true");
    return node;
  }

  function structured(result) {
    return isObject(result) && isObject(result.structuredContent) ? result.structuredContent : null;
  }

  function viewData(result) {
    var meta = isObject(result) && isObject(result._meta) ? result._meta : {};
    if (isObject(meta[VIEW_KEY])) return meta[VIEW_KEY];
    // The server sends none, but a host may pass the data on as structuredContent.
    var content = structured(result);
    if (content && isObject(content[VIEW_KEY])) return content[VIEW_KEY];
    return content && content.view === VIEW_NAME ? content : null;
  }

  function waitForInput(timeoutMs) {
    if (state.toolInput !== null) return Promise.resolve(state.toolInput);
    return new Promise(function (resolve, reject) {
      var wake = function () {
        clearTimeout(timer);
        resolve(state.toolInput);
      };
      var timer = setTimeout(function () {
        inputWaiters = inputWaiters.filter(function (waiter) {
          return waiter !== wake;
        });
        reject(new Error("The host did not send this view's arguments, so it cannot load them."));
      }, timeoutMs);
      inputWaiters.push(wake);
    });
  }

  // Some hosts, such as Claude Desktop, pass a view only a result's content and isError, so
  // its data never arrives. The view then reads it again through the host: its own tool, with
  // the arguments the host delivered in tool-input. A view's tools/call returns the whole
  // result, _meta included.
  function startReread() {
    var entry = { settled: false, generation: generation };
    var settle = function () {
      entry.settled = true;
    };
    // The read is tried even when the host did not declare serverTools, as the MCP Apps SDK's
    // callServerTool does; such a host gets a shorter wait for an answer.
    var declared = can("serverTools");
    // A re-read for arguments the host has since replaced never settles: the newer call's own
    // load updates the view.
    var current = function () {
      return entry.generation === generation;
    };
    entry.promise = waitForInput(5000)
      .then(function (args) {
        return callTool(VIEW_TOOL, args, declared ? 150000 : 30000).catch(function (error) {
          if (error.code === -32601 || (!declared && error.timedOut)) {
            throw new Error(
              "This host did not pass the view its data and does not let views call tools. " +
                "The assistant still has the text result."
            );
          }
          throw error;
        });
      })
      .then(function (result) {
        var data = viewData(result);
        if (!data) {
          throw new Error(
            "The host did not pass the view its data. The assistant still has the text result."
          );
        }
        return data;
      })
      .then(
        function (data) {
          if (!current()) return new Promise(function () {});
          reloaded = data;
          return data;
        },
        function (error) {
          if (!current()) return new Promise(function () {});
          throw error;
        }
      );
    entry.promise.then(settle, settle);
    reread = entry;
    return entry.promise;
  }

  // A delivered result's view data, else the data of one re-read: at most one per call,
  // however often the host delivers the result.
  function load(result) {
    var data = viewData(result);
    if (data) return Promise.resolve(data);
    if (reloaded) return Promise.resolve(reloaded);
    return reread ? reread.promise : startReread();
  }

  // The user's Retry after a failed load: a new re-read, never two at once.
  function retry() {
    return reread && !reread.settled ? reread.promise : startReread();
  }

  // A failed load as the view shows it: what went wrong, and a Retry button.
  function loadFailure(text, onRetry) {
    var box = el("div", "banner error load-failure");
    box.setAttribute("role", "alert");
    box.appendChild(el("div", null, text));
    var again = el("button", null, "Retry");
    again.type = "button";
    again.addEventListener("click", function () {
      again.disabled = true;
      again.textContent = "Loading…";
      onRetry();
    });
    box.appendChild(again);
    return box;
  }

  function readData(id) {
    var node = document.getElementById(id);
    if (!node) return null;
    try {
      return JSON.parse(node.textContent);
    } catch (error) {
      return null;
    }
  }

  return {
    state: state,
    connect: connect,
    callTool: callTool,
    can: can,
    sendMessage: sendMessage,
    updateModelContext: updateModelContext,
    openLink: openLink,
    requestDisplayMode: requestDisplayMode,
    el: el,
    clear: clear,
    safeUrl: safeUrl,
    formatDate: formatDate,
    formatSize: formatSize,
    fileDetails: fileDetails,
    splitAddress: splitAddress,
    avatar: avatar,
    structured: structured,
    viewData: viewData,
    load: load,
    retry: retry,
    loadFailure: loadFailure,
    readData: readData,
  };
})();
