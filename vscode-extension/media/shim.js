/*
 * AgentUI webview shim.
 *
 * The frontend (app.js) talks to the backend with ROOT-RELATIVE URLs
 * (fetch("/api/..."), <img src="/raw?...">, <iframe src="/file?...">). Inside a
 * VSCode webview the document origin is vscode-webview://… so those would never
 * reach the local server. This shim rewrites every root-relative URL to the real
 * backend base (http://127.0.0.1:<port>) WITHOUT modifying app.js.
 *
 * Loaded before app.js. The extension injects window.__AGENTUI_API_BASE__ first.
 */
(function () {
  "use strict";
  var BASE = window.__AGENTUI_API_BASE__;
  if (!BASE) {
    console.error("[agentui-shim] no API base injected; backend calls will fail");
    return;
  }
  if (BASE.charAt(BASE.length - 1) === "/") BASE = BASE.slice(0, -1);

  // Rewrite a "/foo" style URL to BASE + "/foo". Leaves everything else alone.
  function abs(url) {
    if (typeof url !== "string") return url;
    if (url.charAt(0) === "/" && url.charAt(1) !== "/") return BASE + url;
    return url;
  }

  // --- fetch (covers normal API calls AND the streaming-fetch SSE) ----------
  var origFetch = window.fetch ? window.fetch.bind(window) : null;
  if (origFetch) {
    window.fetch = function (input, init) {
      try {
        if (typeof input === "string") {
          return origFetch(abs(input), init);
        }
        if (input && input.url) {
          // A Request object. Rebuild only if it is a same-(webview)-origin
          // root-relative URL that resolved against the webview document.
          var u = new URL(input.url, location.href);
          if (u.origin === location.origin) {
            return origFetch(BASE + u.pathname + u.search + u.hash, init);
          }
        }
      } catch (e) { /* fall through to original */ }
      return origFetch(input, init);
    };
  }

  // --- EventSource (not used today, but future-proof) -----------------------
  if (window.EventSource) {
    var OrigES = window.EventSource;
    function PatchedES(url, cfg) { return new OrigES(abs(url), cfg); }
    PatchedES.prototype = OrigES.prototype;
    PatchedES.CONNECTING = OrigES.CONNECTING;
    PatchedES.OPEN = OrigES.OPEN;
    PatchedES.CLOSED = OrigES.CLOSED;
    window.EventSource = PatchedES;
  }

  // --- DOM resource attributes (img / iframe / embed / source / a[href]) ----
  // The file viewer sets these directly, so fetch wrapping does not catch them.
  var ATTRS = { IMG: "src", IFRAME: "src", EMBED: "src", SOURCE: "src", A: "href", LINK: "href", SCRIPT: "src" };
  function fixEl(el) {
    var attr = ATTRS[el.tagName];
    if (!attr) return;
    var v = el.getAttribute(attr);
    if (v && v.charAt(0) === "/" && v.charAt(1) !== "/") {
      el.setAttribute(attr, BASE + v);
    }
  }
  function scan(node) {
    if (node.nodeType !== 1) return;
    fixEl(node);
    if (node.querySelectorAll) {
      var kids = node.querySelectorAll("img[src],iframe[src],embed[src],source[src],a[href]");
      for (var i = 0; i < kids.length; i++) fixEl(kids[i]);
    }
  }
  if (window.MutationObserver) {
    new MutationObserver(function (muts) {
      for (var i = 0; i < muts.length; i++) {
        var m = muts[i];
        if (m.type === "attributes") { fixEl(m.target); continue; }
        for (var j = 0; j < m.addedNodes.length; j++) scan(m.addedNodes[j]);
      }
    }).observe(document.documentElement, {
      childList: true, subtree: true,
      attributes: true, attributeFilter: ["src", "href"]
    });
  }
})();
