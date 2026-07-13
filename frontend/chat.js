/* ============================================================
   المساعد الجامعي — chat page logic
   ============================================================ */
(() => {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);

  // ---------- auth guard ----------
  let auth;
  try { auth = JSON.parse(sessionStorage.getItem("iug_auth") || "null"); } catch (_) {}
  if (!auth || (!auth.token && auth.role !== "guest")) {
    window.location.replace("index.html");
    return;
  }

  function endSession(message) {
    try { sessionStorage.removeItem("iug_auth"); } catch (_) {}
    try { sessionStorage.setItem("iug_flash", message || ""); } catch (_) {}
    window.location.replace("index.html");
  }

  const el = {
    scroll:   $("#scroll"),
    welcome:  $("#welcome"),
    whoami:   $("#whoami"),
    chips:    $("#chips"),
    composer: $("#composer"),
    input:    $("#q"),
    send:     $("#send"),
    logout:   $("#logout"),
    portal:   $("#portalLink"),
  };

  const role = auth.role || "student";
  const roleNames = { guest: "زائر", student: "طالب", employee: "موظف", admin: "أدمن" };
  const firstName = (auth.name || "").split(" ")[0] || roleNames[role];
  el.whoami.textContent = `${auth.name || roleNames[role]}${role === "guest" ? "" : ` · ${auth.user_id || auth.student_id}`}`;
  el.welcome.textContent = `أهلاً ${firstName} 👋\nأنا مساعد الجامعة الإسلامية بغزة. سأجيبك ضمن صلاحية ${roleNames[role]} فقط.`;
  if (role === "employee" || role === "admin") {
    el.portal.hidden = false;
    el.portal.href = role === "admin" ? "admin.html" : "employee.html";
    el.portal.textContent = role === "admin" ? "لوحة الإدارة" : "لوحة العمل";
  }
  const roleChips = document.querySelectorAll(".chip");
  if (role === "guest" && roleChips[0]) roleChips[0].remove();
  if (role !== "guest" && roleChips[0]) roleChips[0].hidden = false;
  if (role === "employee" && roleChips[0]) roleChips[0].textContent = "ابحث عن بيانات طالب أكاديمية";
  if (role === "admin" && roleChips[0]) roleChips[0].textContent = "ما حالة ملفات المعرفة؟";

  // ---------- lightweight, XSS-safe markdown (only what the bot emits) ------
  function escapeHtml(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  function inline(s) {
    s = escapeHtml(s);
    s = s.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");        // **bold**
    s = s.replace(/(https?:\/\/[^\s<]+)/g,
                  '<a href="$1" target="_blank" rel="noopener">$1</a>');
    s = s.replace(/([\w.+-]+@[\w-]+\.[\w.-]+)/g, '<a href="mailto:$1">$1</a>');
    return s;
  }
  function renderMarkdown(text) {
    const lines = String(text).split("\n");
    let html = "", listType = null, buf = [];
    const flush = () => {
      if (listType) { html += `<${listType}>${buf.join("")}</${listType}>`; buf = []; listType = null; }
    };
    for (const raw of lines) {
      const line = raw.trim();
      const bullet = line.match(/^[-•*]\s+(.*)/);
      const numbered = line.match(/^\d+[.)]\s+(.*)/);
      if (bullet) {
        if (listType !== "ul") { flush(); listType = "ul"; }
        buf.push(`<li>${inline(bullet[1])}</li>`);
      } else if (numbered) {
        if (listType !== "ol") { flush(); listType = "ol"; }
        buf.push(`<li>${inline(numbered[1])}</li>`);
      } else {
        flush();
        if (line) html += `<p>${inline(line)}</p>`;
      }
    }
    flush();
    return html;
  }

  // ---------- helpers ----------
  function bubble(text, who) {
    const wrap = document.createElement("div");
    wrap.className = `msg msg--${who}`;
    if (who === "bot") {
      const ava = document.createElement("span");
      ava.className = "msg__ava"; ava.textContent = "✦"; ava.setAttribute("aria-hidden", "true");
      wrap.appendChild(ava);
    }
    const b = document.createElement("div");
    b.className = "msg__bubble";
    // bot output is trusted-format markdown (escaped first); user text stays plain
    if (who === "bot") b.innerHTML = renderMarkdown(text);
    else b.textContent = text;
    wrap.appendChild(b);
    el.scroll.appendChild(wrap);
    scrollToEnd();
    return b;
  }

  function typing() {
    const wrap = document.createElement("div");
    wrap.className = "msg msg--bot";
    wrap.innerHTML =
      '<span class="msg__ava" aria-hidden="true">✦</span>' +
      '<div class="msg__bubble msg__typing"><span></span><span></span><span></span></div>';
    el.scroll.appendChild(wrap);
    scrollToEnd();
    return wrap;
  }

  function scrollToEnd() { el.scroll.scrollTo({ top: el.scroll.scrollHeight, behavior: "smooth" }); }

  // ---------- send ----------
  let busy = false;

  async function ask(question) {
    if (busy || !question.trim()) return;
    busy = true;
    el.send.disabled = true;
    if (el.chips) el.chips.remove();

    bubble(question, "me");
    el.input.value = "";
    const dots = typing();

    try {
      const endpoint = role === "guest" ? "/api/chat/guest" : "/api/chat";
      const headers = { "Content-Type": "application/json" };
      if (auth.token) headers.Authorization = "Bearer " + auth.token;
      const res = await fetch(endpoint, {
        method: "POST",
        headers,
        body: JSON.stringify({ question }),
      });
      let data = null;
      try { data = await res.json(); } catch (_) {}
      dots.remove();

      if (res.status === 401) {           // token missing/expired → re-login
        endSession("انتهت صلاحية جلستك، سجّل الدخول من جديد.");
        return;
      }
      if (res.ok && data) {
        bubble(data.answer || "لم أستطع إيجاد إجابة.", "bot");
      } else {
        const msg = (data && data.error && data.error.message) ||
                    "تعذّر الحصول على إجابة. حاول مجدداً.";
        bubble("⚠️ " + msg, "bot");
      }
    } catch (_) {
      dots.remove();
      bubble("⚠️ تعذّر الاتصال بالخادم — تحقّق من الإنترنت وحاول مجدداً.", "bot");
    } finally {
      busy = false;
      el.send.disabled = false;
      el.input.focus();
    }
  }

  el.composer.addEventListener("submit", (e) => { e.preventDefault(); ask(el.input.value); });
  document.querySelectorAll(".chip").forEach((c) =>
    c.addEventListener("click", () => ask(c.textContent))
  );

  // ---------- logout ----------
  el.logout.addEventListener("click", () => {
    sessionStorage.removeItem("iug_auth");
    window.location.replace("index.html");
  });

  el.input.focus();
})();
