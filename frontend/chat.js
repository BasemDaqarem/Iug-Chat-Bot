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
  const MAX_QUESTIONS = 20;
  const questionCountKey = `iug_question_count:${role}:${auth.user_id || auth.student_id || "guest"}`;
  let completedQuestions = 0;
  let limitResetInProgress = false;
  let showNewConversationNotice = false;
  try {
    const storedCount = Number.parseInt(sessionStorage.getItem(questionCountKey) || "0", 10);
    completedQuestions = Number.isFinite(storedCount)
      ? Math.max(0, Math.min(MAX_QUESTIONS, storedCount))
      : 0;
    showNewConversationNotice = sessionStorage.getItem("iug_new_chat_notice") === "1";
    sessionStorage.removeItem("iug_new_chat_notice");
  } catch (_) {}
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
  // جداول Markdown لا تُعرض في فقاعات الشات — حوّلها لقائمة نقطية قبل التصيير
  // (تغطي البثّ الحيّ أيضاً حيث يصل الجدول تدريجياً من النموذج).
  function tablesToLists(text) {
    const lines = String(text).split("\n");
    const out = []; let i = 0;
    const isRow = (s) => /^\s*\|.*\|\s*$/.test(s);
    while (i < lines.length) {
      if (!isRow(lines[i])) { out.push(lines[i]); i++; continue; }
      const rows = [];
      while (i < lines.length && isRow(lines[i])) {
        const cells = lines[i].trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim());
        if (!cells.every(c => /^[:\- ]*$/.test(c))) rows.push(cells);  // تجاهل صف الفواصل
        i++;
      }
      if (!rows.length) continue;
      const headers = rows[0], data = rows.slice(1);
      if (!data.length) { out.push("- " + headers.filter(Boolean).join(" — ")); continue; }
      for (const row of data) {
        const rest = [];
        for (let j = 1; j < Math.min(headers.length, row.length); j++)
          if (row[j]) rest.push(`${headers[j]}: ${row[j]}`);
        out.push(`- **${row[0] || ""}**` + (rest.length ? ` — ${rest.join("، ")}` : ""));
      }
    }
    return out.join("\n");
  }

  function renderMarkdown(text) {
    const lines = tablesToLists(String(text)).split("\n");
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
      ava.className = "msg__ava"; ava.textContent = "ج"; ava.setAttribute("aria-hidden", "true");
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
      '<span class="msg__ava" aria-hidden="true">ج</span>' +
      '<div class="msg__bubble msg__typing"><span></span><span></span><span></span></div>';
    el.scroll.appendChild(wrap);
    scrollToEnd();
    return wrap;
  }

  function scrollToEnd() { el.scroll.scrollTo({ top: el.scroll.scrollHeight, behavior: "smooth" }); }

  if (showNewConversationNotice) {
    bubble("بدأت محادثة جديدة بسجل فارغ. يمكنك طرح سؤالك الآن.", "bot");
  }

  // ---------- send ----------
  let busy = false;

  async function resetConversationAfterLimit() {
    if (limitResetInProgress) return;
    limitResetInProgress = true;
    el.input.disabled = true;
    el.send.disabled = true;
    bubble(
      "وصلت إلى 20 سؤالًا. سأمسح سجل هذه المحادثة وأبدأ محادثة جديدة.",
      "bot"
    );
    try {
      if (role === "guest") {
        guestHistory.length = 0;
      } else {
        const res = await fetch("/api/sessions/me/history", {
          method: "DELETE",
          headers: { Authorization: "Bearer " + auth.token },
        });
        if (res.status === 401) {
          endSession("انتهت صلاحية جلستك، سجّل الدخول من جديد.");
          return;
        }
        if (!res.ok) throw new Error("history clear failed");
      }
      completedQuestions = 0;
      try { sessionStorage.setItem(questionCountKey, "0"); } catch (_) {}
      try { sessionStorage.setItem("iug_new_chat_notice", "1"); } catch (_) {}
      window.setTimeout(() => window.location.reload(), 900);
    } catch (_) {
      limitResetInProgress = false;
      el.send.disabled = false;
      bubble(
        "⚠️ تعذّر مسح سجل المحادثة، لذلك لم أُحدّث الصفحة. اضغط إرسال لإعادة محاولة المسح.",
        "bot"
      );
    }
  }

  async function ask(question) {
    if (busy) return;
    if (completedQuestions >= MAX_QUESTIONS) {
      await resetConversationAfterLimit();
      return;
    }
    if (!question.trim()) return;
    busy = true;
    el.send.disabled = true;
    if (el.chips) el.chips.remove();

    bubble(question, "me");
    el.input.value = "";
    const dots = typing();

    try {
      // Signed-in students get the token-by-token stream; guests use the
      // plain endpoint (no token to stream under).
      let completed = false;
      if (auth.token && role !== "guest") {
        completed = await streamAsk(question, dots);
      } else {
        completed = await plainAsk(question, dots);
      }
      if (completed) {
        completedQuestions += 1;
        try {
          sessionStorage.setItem(questionCountKey, String(completedQuestions));
        } catch (_) {}
        if (completedQuestions >= MAX_QUESTIONS) {
          await resetConversationAfterLimit();
        }
      }
    } catch (_) {
      dots.remove();
      bubble("⚠️ تعذّر الاتصال بالخادم — تحقّق من الإنترنت وحاول مجدداً.", "bot");
    } finally {
      busy = false;
      if (!limitResetInProgress && completedQuestions < MAX_QUESTIONS) {
        el.input.disabled = false;
        el.send.disabled = false;
        el.input.focus();
      }
    }
  }

  // Stream: render the answer as it arrives, growing the same bubble.
  async function streamAsk(question, dots) {
    const res = await fetch("/api/chat/student/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: "Bearer " + auth.token },
      body: JSON.stringify({ question }),
    });
    if (res.status === 401) {            // token missing/expired → re-login
      dots.remove();
      endSession("انتهت صلاحية جلستك، سجّل الدخول من جديد.");
      return false;
    }
    if (!res.ok || !res.body) {          // pre-stream error (e.g. 429) = JSON
      dots.remove();
      let data = null; try { data = await res.json(); } catch (_) {}
      bubble("⚠️ " + ((data && data.error && data.error.message) ||
                      "تعذّر الحصول على إجابة. حاول مجدداً."), "bot");
      return false;
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let full = "", botBubble = null;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      full += decoder.decode(value, { stream: true });
      if (!botBubble) { dots.remove(); botBubble = bubble("", "bot"); }
      botBubble.innerHTML = renderMarkdown(full);
      scrollToEnd();
    }
    if (!botBubble) {
      dots.remove();
      bubble("لم أستطع إيجاد إجابة.", "bot");
      return false;
    }
    return Boolean(full.trim()) && !full.trim().startsWith("⚠️");
  }

  // Non-stream fallback for guests.
  // الزوار بلا جلسات على الخادم — نحمل آخر 5 أدوار محلياً ونرسلها مع كل
  // سؤال ليفهم البوت المتابعات («هل ممكن انقبل بالتمريض؟» بعد ذكر المعدل).
  const guestHistory = [];
  async function plainAsk(question, dots) {
    const res = await fetch("/api/chat/guest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, history: guestHistory.slice(-5) }),
    });
    let data = null; try { data = await res.json(); } catch (_) {}
    dots.remove();
    if (res.ok && data) {
      const answer = data.answer || "لم أستطع إيجاد إجابة.";
      bubble(answer, "bot");
      guestHistory.push({ user: question, assistant: answer });
      if (guestHistory.length > 5) guestHistory.shift();
      return true;
    } else {
      bubble("⚠️ " + ((data && data.error && data.error.message) ||
                      "تعذّر الحصول على إجابة. حاول مجدداً."), "bot");
      return false;
    }
  }

  el.composer.addEventListener("submit", (e) => { e.preventDefault(); ask(el.input.value); });
  document.querySelectorAll(".chip").forEach((c) =>
    c.addEventListener("click", () => ask(c.textContent))
  );
  if (completedQuestions >= MAX_QUESTIONS) {
    window.setTimeout(() => resetConversationAfterLimit(), 0);
  }

  // ---------- logout ----------
  el.logout.addEventListener("click", () => {
    sessionStorage.removeItem("iug_auth");
    window.location.replace("index.html");
  });

  el.input.focus();
})();
