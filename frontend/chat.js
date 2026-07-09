/* ============================================================
   المساعد الجامعي — chat page logic
   ============================================================ */
(() => {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);

  // ---------- auth guard ----------
  let auth;
  try { auth = JSON.parse(sessionStorage.getItem("iug_auth") || "null"); } catch (_) {}
  if (!auth || !auth.token) {           // no signed session → back to login
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
  };

  const firstName = (auth.name || "").split(" ")[0] || "الطالب";
  el.whoami.textContent = `${auth.name || "طالب"} · ${auth.student_id}`;
  el.welcome.textContent =
    `أهلاً ${firstName} 👋\nأنا مساعدك في الجامعة الإسلامية بغزة. اسألني عن الرسوم، ` +
    `الكليات، التخصصات، القبول، المنح، أو أي خدمة طلابية.`;

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
    b.textContent = text;
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
      const res = await fetch("/api/chat/student", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Authorization": "Bearer " + auth.token,   // identity from the token
        },
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
