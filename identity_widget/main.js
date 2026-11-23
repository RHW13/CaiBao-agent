/* 身份识别组件：把「设备唯一标识 + 登录令牌」持久化在浏览器 localStorage。
   组件本身不可见（高度 0），只负责读取 / 写入并把结果回传给 Streamlit。 */
(function () {
  "use strict";

  var DEVICE_KEY = "cb_device_id";
  var TOKEN_KEY = "cb_session_token";

  function post(type, payload) {
    var data = { isStreamlitMessage: true, type: type };
    for (var k in payload) {
      if (Object.prototype.hasOwnProperty.call(payload, k)) data[k] = payload[k];
    }
    window.parent.postMessage(data, "*");
  }

  function setValue(value) {
    post("streamlit:setComponentValue", { value: value, dataType: "json" });
  }

  function uuid() {
    try {
      if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    } catch (e) { /* noop */ }
    return "xxxxxxxxxxxx4xxxyxxxxxxxxxxxxxxx".replace(/[xy]/g, function (c) {
      var r = (Math.random() * 16) | 0;
      return (c === "x" ? r : (r & 3) | 8).toString(16);
    }) + Date.now().toString(16);
  }

  function readDevice() {
    try {
      var d = localStorage.getItem(DEVICE_KEY);
      if (!d) { d = uuid(); localStorage.setItem(DEVICE_KEY, d); }
      return d;
    } catch (e) { return ""; }
  }

  function readToken() {
    try { return localStorage.getItem(TOKEN_KEY) || ""; } catch (e) { return ""; }
  }

  function writeToken(token) {
    try {
      if (token) localStorage.setItem(TOKEN_KEY, token);
      else localStorage.removeItem(TOKEN_KEY);
    } catch (e) { /* noop */ }
  }

  function snapshot() {
    return { device_id: readDevice(), token: readToken() };
  }

  post("streamlit:componentReady", { apiVersion: 1 });
  post("streamlit:setFrameHeight", { height: 0 });

  window.addEventListener("message", function (event) {
    var data = event.data;
    if (!data || data.type !== "streamlit:render") return;
    var args = data.args || {};
    if (args.clear_token) writeToken("");
    if (args.set_token) writeToken(String(args.set_token));
    setValue(snapshot());
  });

  // 兜底：若未收到 render（极端情况），也要把设备标识回传一次
  setTimeout(function () { setValue(snapshot()); }, 1200);
})();
