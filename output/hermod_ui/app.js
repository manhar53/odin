/* ============================================================
   HERMOD — corner avatar + ChatGPT/Claude-style chat thread.

   Drag handling (single avatar handles both click + drag):
     mousedown               → start a 300 ms "is this a drag?" timer
     move >5 px before timer → it's a drag; tell Python to reposition
     mouseup before timer    → it was a click; toggle the panel

   Python -> JS bridge:
     hermod.setState(state, payload)   idle | listening | thinking | speaking
     hermod.speakChunk(text)           append/extend the current ODIN bubble
     hermod.addUserMessage(text)       append a user bubble (typed or uploaded)
     hermod.beginTurn()                start a fresh ODIN reply on next chunk
     hermod.clearReply()               alias of beginTurn (back-compat)
     hermod.collapse() / expand()

   JS -> Python bridge:
     pywebview.api.submit_text(text)   user typed and hit Enter
     pywebview.api.upload()            user tapped + (opens a file picker)
     pywebview.api.toggle_throne()     clicked the World Tree button
     pywebview.api.start_drag(sx, sy)  drag begun at screen coord
     pywebview.api.drag_to(sx, sy)     fired ~60Hz while dragging
     pywebview.api.end_drag()          mouse released
   ============================================================ */

(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const body  = document.body;
  const avatar = $("avatar");
  const panel  = $("panel");
  const thread = $("thread");
  const inputBox = $("inputBox");
  const form   = $("inputForm");
  const closeBtn = $("closeBtn");
  const treeBtn  = $("treeBtn");
  const uploadBtn = $("uploadBtn");

  // The ODIN bubble currently being filled by speakChunk. Reset at the start
  // of each turn so a new reply opens a new bubble instead of appending to the
  // last one. null = the next chunk creates a fresh bubble.
  let odinBubble = null;
  let thinkingEl = null;

  function clearEmpty() {
    const e = thread.querySelector(".thread-empty");
    if (e) e.remove();
  }
  function scrollDown() {
    requestAnimationFrame(() => { thread.scrollTop = thread.scrollHeight; });
  }
  function removeThinking() {
    if (thinkingEl) { thinkingEl.remove(); thinkingEl = null; }
  }

  // ── Public API ──────────────────────────────────────────────
  function setStateClass(state) {
    body.classList.remove("idle", "listening", "thinking", "speaking");
    body.classList.add(state);
  }

  function addUserMessage(text, isAttach) {
    if (!text) return;
    clearEmpty();
    removeThinking();
    const el = document.createElement("div");
    el.className = "msg user" + (isAttach ? " attach" : "");
    el.textContent = text;
    thread.appendChild(el);
    odinBubble = null;      // ODIN's answer starts a new bubble
    showThinking();
    expand();
    scrollDown();
  }

  function showThinking() {
    removeThinking();
    thinkingEl = document.createElement("div");
    thinkingEl.className = "msg odin thinking";
    thinkingEl.innerHTML = "<span></span><span></span><span></span>";
    thread.appendChild(thinkingEl);
    scrollDown();
  }

  function speakChunk(text) {
    if (!text) return;
    clearEmpty();
    removeThinking();
    if (!odinBubble) {
      odinBubble = document.createElement("div");
      odinBubble.className = "msg odin";
      odinBubble.textContent = text;
      thread.appendChild(odinBubble);
    } else {
      odinBubble.textContent = (odinBubble.textContent || "").trimEnd() + " " + text;
    }
    expand();
    scrollDown();
  }

  // New turn — the next speakChunk opens a fresh ODIN bubble.
  function beginTurn() { odinBubble = null; }

  function setState(state, payload) {
    setStateClass(state);
    if (state === "speaking" && payload && payload.text) {
      speakChunk(payload.text);
    } else if (state === "listening") {
      // Voice turn beginning — close off any prior reply and show we're
      // working. Don't force-expand: during voice ODIN may be hidden and
      // the World Tree is carrying the conversation.
      odinBubble = null;
      showThinking();
    } else if (state === "idle") {
      removeThinking();
    }
  }

  function expand()   { body.classList.remove("collapsed"); panel.setAttribute("aria-hidden", "false"); }
  function collapse() { body.classList.add("collapsed");    panel.setAttribute("aria-hidden", "true");
                        inputBox.blur(); }

  window.hermod = {
    setState, speakChunk, addUserMessage, beginTurn,
    clearReply: beginTurn,            // back-compat alias
    expand, collapse,
    ping: () => "hermod-alive",
  };

  // ── Drag vs click on avatar ─────────────────────────────────
  let downAt = 0;
  let downScreenX = 0, downScreenY = 0;
  let isDragging = false;
  let dragTimer = null;
  const DRAG_DELAY = 300;
  const DRAG_THRESHOLD_PX = 5;

  function onAvatarDown(e) {
    if (e.button !== 0 && e.button !== 2) return;
    downAt = Date.now();
    downScreenX = e.screenX;
    downScreenY = e.screenY;
    isDragging = false;
    dragTimer = setTimeout(() => beginDrag(downScreenX, downScreenY), DRAG_DELAY);
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup",   onUp, { once: true });
    e.preventDefault();
  }

  function beginDrag(sx, sy) {
    if (isDragging) return;
    isDragging = true;
    body.classList.add("dragging");
    if (window.pywebview && window.pywebview.api) {
      try { window.pywebview.api.start_drag(sx, sy); } catch (_) {}
    }
  }

  function onMove(e) {
    if (!isDragging) {
      const dx = e.screenX - downScreenX;
      const dy = e.screenY - downScreenY;
      if (Math.hypot(dx, dy) > DRAG_THRESHOLD_PX) {
        if (dragTimer) clearTimeout(dragTimer);
        beginDrag(downScreenX, downScreenY);
      } else {
        return;
      }
    }
    if (window.pywebview && window.pywebview.api) {
      try { window.pywebview.api.drag_to(e.screenX, e.screenY); } catch (_) {}
    }
  }

  function onUp(e) {
    document.removeEventListener("mousemove", onMove);
    if (dragTimer) { clearTimeout(dragTimer); dragTimer = null; }
    const dt = Date.now() - downAt;
    const dx = e.screenX - downScreenX;
    const dy = e.screenY - downScreenY;
    const wasClick = !isDragging && dt < DRAG_DELAY && Math.hypot(dx, dy) <= DRAG_THRESHOLD_PX;
    if (isDragging) {
      body.classList.remove("dragging");
      if (window.pywebview && window.pywebview.api) {
        try { window.pywebview.api.end_drag(); } catch (_) {}
      }
    } else if (wasClick) {
      if (body.classList.contains("collapsed")) expand();
      else collapse();
    }
    isDragging = false;
  }

  avatar.addEventListener("mousedown", onAvatarDown);
  avatar.addEventListener("contextmenu", (e) => e.preventDefault());

  // ── Panel buttons ───────────────────────────────────────────
  closeBtn.addEventListener("click", (e) => { e.stopPropagation(); collapse(); });

  treeBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    if (window.pywebview && window.pywebview.api) {
      try { window.pywebview.api.toggle_throne(); } catch (_) {}
    }
  });

  uploadBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    if (window.pywebview && window.pywebview.api && window.pywebview.api.upload) {
      try { window.pywebview.api.upload(); } catch (_) {}
    }
  });

  // ── Text input ──────────────────────────────────────────────
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const t = inputBox.value.trim();
    if (!t) return;
    inputBox.value = "";
    addUserMessage(t);
    setStateClass("thinking");
    if (window.pywebview && window.pywebview.api) {
      try { window.pywebview.api.submit_text(t); } catch (_) {}
    }
  });

  // Esc collapses the panel from anywhere (input box included).
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") collapse();
  });

  setStateClass("idle");
})();
