/* ============================================================
   بوابة الطالب — auth flow interactions
   Vanilla JS, no dependencies. Small focused helpers.
   ============================================================ */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const API = ""; // same origin (served by FastAPI at /app/)

  const el = {
    card:    $("#card"),
    switch:  $(".switch"),
    thumb:   $("#thumb"),
    opts:    document.querySelectorAll(".switch__opt"),
    form:    $("#authForm"),
    name:    $("#name"),
    id:      $("#student_id"),
    pass:    $("#password"),
    eye:     $("#eye"),
    submit:  $("#submit"),
    label:   $(".submit__label"),
    msg:     $("#formMsg"),
    foots:   document.querySelectorAll("[data-foot]"),
  };

  let mode = "login"; // 'login' | 'register'

  // ---------- mode switching (login <-> register) ----------
  const COPY = {
    login:    { btn: "تسجيل الدخول",  pass: "current-password" },
    register: { btn: "إنشاء الحساب",  pass: "new-password" },
  };

  function setMode(next) {
    if (next === mode) return;
    mode = next;
    el.switch.classList.toggle("is-register", mode === "register");
    el.opts.forEach((o) => o.classList.toggle("is-active", o.dataset.mode === mode));
    el.label.textContent = COPY[mode].btn;
    el.pass.setAttribute("autocomplete", COPY[mode].pass);
    el.foots.forEach((f) => (f.hidden = f.dataset.foot !== mode));
    clearMessage();
    clearErrors();
  }

  el.opts.forEach((o) => o.addEventListener("click", () => setMode(o.dataset.mode)));
  document.querySelectorAll("[data-goto]").forEach((a) =>
    a.addEventListener("click", (e) => { e.preventDefault(); setMode(a.dataset.goto); })
  );

  // ---------- input polish ----------
  // Student ID: digits only.
  el.id.addEventListener("input", () => {
    const clean = el.id.value.replace(/\D+/g, "");
    if (clean !== el.id.value) el.id.value = clean;
    clearFieldError(el.id);
  });
  el.name.addEventListener("input", () => clearFieldError(el.name));
  el.pass.addEventListener("input", () => clearFieldError(el.pass));

  // Password show/hide.
  el.eye.addEventListener("click", () => {
    const show = el.pass.type === "password";
    el.pass.type = show ? "text" : "password";
    el.eye.classList.toggle("is-on", show);
    el.eye.setAttribute("aria-label", show ? "إخفاء كلمة المرور" : "إظهار كلمة المرور");
  });

  // ---------- validation ----------
  function fieldOf(input) { return input.closest(".field"); }

  function setFieldError(input, message) {
    const field = fieldOf(input);
    field.classList.add("has-error");
    const err = field.querySelector(".field__err");
    if (err) err.textContent = message;
  }
  function clearFieldError(input) {
    const field = fieldOf(input);
    field.classList.remove("has-error");
    const err = field.querySelector(".field__err");
    if (err) err.textContent = "";
  }
  function clearErrors() {
    [el.name, el.id, el.pass].forEach(clearFieldError);
  }

  function validate() {
    let ok = true;
    if (mode === "register" && el.name.value.trim().length < 2) {
      setFieldError(el.name, "أدخل اسمك (حرفان على الأقل)."); ok = false;
    }
    if (!/^\d{3,20}$/.test(el.id.value)) {
      setFieldError(el.id, "الرقم الجامعي أرقام فقط (3 خانات على الأقل)."); ok = false;
    }
    if (el.pass.value.length < 4) {
      setFieldError(el.pass, "كلمة المرور 4 أحرف على الأقل."); ok = false;
    }
    return ok;
  }

  // ---------- messages ----------
  function showMessage(text, kind) {
    el.msg.textContent = text;
    el.msg.className = "form__msg show " + (kind || "");
  }
  function clearMessage() { el.msg.textContent = ""; el.msg.className = "form__msg"; }

  // ---------- submit (with button morph) ----------
  let busy = false;

  async function onSubmit(e) {
    e.preventDefault();
    if (busy) return;
    clearMessage();
    if (!validate()) return;

    busy = true;
    el.submit.classList.add("is-loading");
    el.submit.disabled = true;

    const path = mode === "login" ? "/api/auth/login" : "/api/auth/register";
    const payload = { student_id: el.id.value, password: el.pass.value };
    if (mode === "register") payload.name = el.name.value.trim();

    try {
      const res = await postJSON(API + path, payload);
      const started = performance.now();
      // keep the loading morph visible a beat for a premium feel
      await minDelay(started, 550);

      if (res.ok) {
        onSuccess(res.data);
      } else {
        onFailure(res.status, res.data);
      }
    } catch (err) {
      onNetworkError();
    } finally {
      busy = false;
    }
  }

  function onSuccess(data) {
    el.submit.classList.remove("is-loading");
    el.submit.classList.add("is-done");
    const name = (data && data.profile && data.profile.name) || "";
    const greet = mode === "login" ? "مرحباً بعودتك" : "تم إنشاء حسابك";
    showMessage(name ? `${greet}، ${name} — جارٍ التحويل…` : "جارٍ التحويل…", "ok");
    // Carry the authenticated identity to the chat page and redirect.
    try {
      sessionStorage.setItem("iug_auth", JSON.stringify({
        student_id: (data && data.student_id) || el.id.value,
        name: name,
        profile: (data && data.profile) || {},
      }));
    } catch (_) {}
    setTimeout(() => { window.location.href = "chat.html"; }, 900);
  }

  function onFailure(status, data) {
    resetButton();
    // Unified error envelope: { success:false, error:{ code, message, details } }
    const error = (data && data.error) || {};
    if (status === 422 && Array.isArray(error.details)) {
      error.details.forEach((d) => {
        const input = { name: el.name, student_id: el.id, password: el.pass }[d.field];
        if (input) setFieldError(input, d.message);
      });
      showMessage("تحقّق من الحقول المُعلّمة.", "err");
    } else {
      showMessage(error.message || "تعذّر إتمام العملية. حاول مجدداً.", "err");
    }
  }

  function onNetworkError() {
    resetButton();
    showMessage("تعذّر الاتصال بالخادم — تحقّق من الإنترنت وحاول مجدداً.", "err");
  }

  function resetButton() {
    el.submit.classList.remove("is-loading", "is-done");
    el.submit.disabled = false;
  }

  // ---------- tiny fetch + timing helpers ----------
  async function postJSON(url, body) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    let data = null;
    try { data = await res.json(); } catch (_) {}
    return { ok: res.ok, status: res.status, data };
  }
  function minDelay(start, ms) {
    const elapsed = performance.now() - start;
    return elapsed >= ms ? Promise.resolve() : new Promise((r) => setTimeout(r, ms - elapsed));
  }

  el.form.addEventListener("submit", onSubmit);
})();
