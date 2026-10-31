/* Proton Mail MCP view bridge: MCP Apps @PROTOCOL_VERSION@ JSON-RPC 2.0 over postMessage.
   Views never insert mail-derived text as markup: they build nodes with textContent.
   Only the parent window (the host) is trusted as a message source. */
"use strict";
var PMM = (function () {
  var PROTOCOL_VERSION = "@PROTOCOL_VERSION@";
  // The result _meta key with a view's data; hosts keep _meta out of model context.
  var VIEW_KEY = "@VIEW_META_KEY@";
  var host = window.parent;
  var nextId = 1;
  var pending = new Map();
  var handlers = {};
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
        reject(new Error("The host did not answer " + method));
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
      state.toolInput = isObject(params.arguments) ? params.arguments : {};
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

  function callTool(name, args) {
    return request("tools/call", { name: name, arguments: args || {} }, 150000).then(
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

  function formatDate(value) {
    if (typeof value !== "string" || !value) return "";
    var parsed = Date.parse(value);
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
    return isObject(meta[VIEW_KEY]) ? meta[VIEW_KEY] : null;
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
    readData: readData,
  };
})();
