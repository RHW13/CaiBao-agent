/* 财报问答悬浮组件：组件 iframe 自身即悬浮框（不与页面抢点击），内部负责聊天交互 */
(function () {
  "use strict";

  var mascot = document.getElementById("mascot");
  var panel = document.getElementById("panel");
  var handle = document.getElementById("drag-handle");
  var messagesEl = document.getElementById("messages");
  var input = document.getElementById("input");
  var sendBtn = document.getElementById("send");
  var composer = document.getElementById("composer");
  var companyEl = document.getElementById("panel-company");

  var CLOSED_W = 100, CLOSED_H = 126;
  var state = { messages: [], ready: false, company: "", pending: false, open: false };
  var box = { left: 0, top: 0, w: CLOSED_W, h: CLOSED_H };
  var startDrag = null;

  /* ---------------- 与 Streamlit 通信 ---------------- */
  function post(type, payload) {
    var data = { isStreamlitMessage: true, type: type };
    for (var k in payload) {
      if (Object.prototype.hasOwnProperty.call(payload, k)) data[k] = payload[k];
    }
    window.parent.postMessage(data, "*");
  }
  function setValue(value) { post("streamlit:setComponentValue", { value: value, dataType: "json" }); }

  window.addEventListener("message", function (event) {
    var data = event.data;
    if (!data || data.type !== "streamlit:render") return;
    var args = data.args || {};
    state.messages = Array.isArray(args.messages) ? args.messages : [];
    state.ready = !!args.ready;
    state.company = args.company || "";
    renderState();
  });

  post("streamlit:componentReady", { apiVersion: 1 });

  /* ---------------- 把 iframe 本身变成悬浮框 ---------------- */
  function hostWindow() {
    try { return window.parent; } catch (e) { return window; }
  }

  function applyFrame(left, top, w, h) {
    var fe = null;
    try { fe = window.frameElement; } catch (e) { fe = null; }
    if (!fe) return;                       // 跨域时退回 CSS 选择器方案
    var s = fe.style;
    s.setProperty("position", "fixed", "important");
    s.setProperty("left", left + "px", "important");
    s.setProperty("top", top + "px", "important");
    s.setProperty("right", "auto", "important");
    s.setProperty("bottom", "auto", "important");
    s.setProperty("width", w + "px", "important");
    s.setProperty("height", h + "px", "important");
    s.setProperty("border", "0", "important");
    s.setProperty("background", "transparent", "important");
    s.setProperty("z-index", "9000", "important");
    s.setProperty("pointer-events", "auto", "important");
    s.setProperty("box-shadow", "none", "important");
    var holder = fe.parentElement;
    if (holder) {
      holder.style.height = "0px";
      holder.style.minHeight = "0px";
      holder.style.margin = "0";
      holder.style.padding = "0";
    }
  }

  function viewport() {
    var w = hostWindow();
    return { w: w.innerWidth || 1280, h: w.innerHeight || 800 };
  }

  function clampRect(left, top, w, h) {
    var v = viewport();
    return {
      left: Math.min(Math.max(6, left), Math.max(6, v.w - w - 6)),
      top: Math.min(Math.max(6, top), Math.max(6, v.h - h - 6))
    };
  }

  function readPos() {
    try {
      var raw = sessionStorage.getItem("qa_pos");
      return raw ? JSON.parse(raw) : null;
    } catch (e) { return null; }
  }

  function layout() {
    var v = viewport();
    if (state.open) {
      var w = Math.min(380, v.w - 24);
      var h = Math.min(560, Math.round(v.h * 0.82));
      var pos = readPos() || { left: v.w - w - 16, top: v.h - h - 20 };
      var p = clampRect(pos.left, pos.top, w, h);
      box = { left: p.left, top: p.top, w: w, h: h };
      applyFrame(box.left, box.top, box.w, box.h);
    } else {
      box = { left: v.w - CLOSED_W - 12, top: Math.round((v.h - CLOSED_H) / 2), w: CLOSED_W, h: CLOSED_H };
      applyFrame(box.left, box.top, box.w, box.h);
    }
  }
  layout();

  /* ---------------- 渲染 ---------------- */
  function renderState() {
    companyEl.textContent = state.company || "数字题直接计算，文本题标注页码";
    input.disabled = !state.ready || state.pending;
    sendBtn.disabled = !state.ready || state.pending;
    input.placeholder = state.ready
      ? "例如：营业收入是多少？净利润下降的原因？"
      : "请先在左侧上传年报 PDF";
    renderMessages();
    layout();
    if (sessionStorage.getItem("qa_open") === "1" && !state.open) openPanel(false);
  }

  function renderMessages() {
    messagesEl.innerHTML = "";
    if (!state.messages.length) {
      var empty = document.createElement("div");
      empty.className = "empty";
      empty.innerHTML = state.ready
        ? "<b>可以开始提问了</b>想问营收、净利、ROE，或某段话为什么这么写，<br>直接问我就行，回答会标注页码。"
        : "<b>还没有财报</b>在左侧上传 PDF 后，我就能帮你读数、算指标、找依据。";
      messagesEl.appendChild(empty);
      return;
    }
    state.messages.forEach(function (msg) {
      var item = document.createElement("div");
      item.className = "msg " + (msg.role === "user" ? "user" : "assistant");
      var bubble = document.createElement("div");
      bubble.className = "bubble";
      bubble.textContent = msg.content || "";
      item.appendChild(bubble);

      var cites = msg.citations || [];
      if (cites.length) {
        var wrap = document.createElement("div");
        wrap.className = "cites";
        cites.slice(0, 4).forEach(function (c) {
          var d = document.createElement("details");
          var s = document.createElement("summary");
          s.textContent = "第 " + c.page + " 页";
          var p = document.createElement("p");
          p.textContent = c.snippet || "";
          d.appendChild(s);
          d.appendChild(p);
          wrap.appendChild(d);
        });
        item.appendChild(wrap);
      }
      messagesEl.appendChild(item);
    });
    if (state.pending) {
      var typing = document.createElement("div");
      typing.className = "msg assistant";
      typing.innerHTML = '<div class="bubble dots"><span></span><span></span><span></span></div>';
      messagesEl.appendChild(typing);
    }
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  /* ---------------- 开合 ---------------- */
  function openPanel(remember) {
    state.open = true;
    panel.hidden = false;
    mascot.style.display = "none";
    mascot.setAttribute("aria-expanded", "true");
    document.getElementById("panel-avatar").innerHTML =
      document.getElementById("mascot-art").innerHTML;
    if (remember !== false) sessionStorage.setItem("qa_open", "1");
    mascot.classList.remove("has-unread");
    sessionStorage.setItem("qa_seen", String(state.messages.length));
    layout();
    setTimeout(function () { if (!input.disabled) input.focus(); }, 80);
  }

  function closePanel() {
    state.open = false;
    panel.hidden = true;
    mascot.style.display = "";
    mascot.setAttribute("aria-expanded", "false");
    sessionStorage.setItem("qa_open", "0");
    layout();
  }

  mascot.addEventListener("click", function () { openPanel(true); });
  document.getElementById("btn-close").addEventListener("click", closePanel);
  document.getElementById("btn-min").addEventListener("click", closePanel);
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && state.open) closePanel();
  });

  /* ---------------- 拖拽（screenX/Y 不受 iframe 位移影响） ---------------- */
  handle.addEventListener("pointerdown", function (e) {
    if (e.target.closest("button")) return;
    startDrag = { sx: e.screenX, sy: e.screenY, left: box.left, top: box.top };
    handle.classList.add("dragging");
    try { handle.setPointerCapture(e.pointerId); } catch (err) { /* noop */ }
    e.preventDefault();
  });

  handle.addEventListener("pointermove", function (e) {
    if (!startDrag) return;
    var p = clampRect(
      startDrag.left + (e.screenX - startDrag.sx),
      startDrag.top + (e.screenY - startDrag.sy),
      box.w, box.h
    );
    box.left = p.left;
    box.top = p.top;
    applyFrame(p.left, p.top, box.w, box.h);
  });

  function endDrag(e) {
    if (!startDrag) return;
    startDrag = null;
    handle.classList.remove("dragging");
    try { handle.releasePointerCapture(e.pointerId); } catch (err) { /* noop */ }
    sessionStorage.setItem("qa_pos", JSON.stringify({ left: box.left, top: box.top }));
  }
  handle.addEventListener("pointerup", endDrag);
  handle.addEventListener("pointercancel", endDrag);

  /* ---------------- 发送 ---------------- */
  function submit() {
    var q = input.value.trim();
    if (!q || !state.ready || state.pending) return;
    state.pending = true;
    input.value = "";
    input.style.height = "auto";
    sendBtn.disabled = true;
    renderMessages();
    setValue({ action: "ask", question: q, nonce: String(Date.now()) });
  }

  composer.addEventListener("submit", function (e) { e.preventDefault(); submit(); });
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); }
  });
  input.addEventListener("input", function () {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 80) + "px";
  });

  try {
    window.parent.addEventListener("resize", layout);
  } catch (e) { /* 跨域时忽略 */ }
})();
