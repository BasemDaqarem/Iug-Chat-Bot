(() => {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  let session;
  try { session = JSON.parse(sessionStorage.getItem("iug_auth") || "null"); } catch (_) {}
  if (!session || !session.token || session.role !== "admin") { location.replace("index.html"); return; }

  const state = { files: [], employees: [], audit: [] };
  const titles = { overview: "نظرة عامة", files: "إدارة الملفات", employees: "حسابات الموظفين", audit: "سجل التدقيق" };
  const roleNames = { guest: "زائر", student: "طالب", employee: "موظف", admin: "أدمن" };
  const classNames = { university_public: "جامعة عام", student_records: "سجلات طلاب", employee_internal: "موظفون داخلي", employee_private: "موظف خاص", admin_only: "أدمن فقط" };
  const statusNames = { draft: "مسودة", ready: "جاهز للنشر", published: "منشور", archived: "مؤرشف" };
  // مرآة قواعد الخادم (_sanitize_policy): أقصى أدوار يسمح بها كل تصنيف،
  // والتصنيفات التي تتطلب مالكاً — حتى لا يرتطم الأدمن بأخطاء 400 مفاجئة.
  const classMaxRoles = {
    university_public: ["guest", "student", "employee", "admin"],
    student_records: ["student", "employee", "admin"],   // الموظف يرى سجلات الطلاب
    employee_internal: ["employee", "admin"],
    employee_private: ["employee", "admin"],
    admin_only: ["admin"],
  };
  const ownerRequired = ["student_records", "employee_private"];

  function syncPolicyControls(form) {
    const cls = form.elements.classification.value;
    const allowed = classMaxRoles[cls] || ["admin"];
    $$("input[name=roles]", form).forEach(box => {
      const ok = allowed.includes(box.value);
      box.disabled = !ok;
      if (!ok) box.checked = false;
      else if (allowed.length === 1) box.checked = true;
    });
    const hint = $("[data-roles-hint]", form);
    if (hint) hint.textContent = allowed.length < 4 ? `هذا التصنيف يسمح فقط بـ: ${allowed.map(r => roleNames[r]).join("، ")}` : "";
    const ownerWrap = $("[data-owner-wrap]", form);
    if (ownerWrap) {
      const need = ownerRequired.includes(cls);
      ownerWrap.hidden = !need;
      ownerWrap.querySelector("input").required = need;
    }
  }

  async function api(path, options = {}) {
    const headers = { ...(options.headers || {}), Authorization: `Bearer ${session.token}` };
    if (options.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
    const res = await fetch(path, { ...options, headers });
    let data = null; try { data = await res.json(); } catch (_) {}
    if (res.status === 401) { endSession("انتهت صلاحية الجلسة."); throw new Error("unauthorized"); }
    if (!res.ok) throw new Error(data?.error?.message || "تعذّر إتمام العملية.");
    return data;
  }
  function endSession(message = "") { sessionStorage.removeItem("iug_auth"); sessionStorage.setItem("iug_flash", message); location.replace("index.html"); }
  let toastTimer;
  function toast(message, error = false) { const el = $("#toast"); el.textContent = message; el.className = `toast show${error ? " error" : ""}`; clearTimeout(toastTimer); toastTimer = setTimeout(() => el.className = "toast", 3200); }
  function el(tag, className, text) { const node = document.createElement(tag); if (className) node.className = className; if (text != null) node.textContent = text; return node; }
  function cell(content) { const node = el("td"); if (content instanceof Node) node.append(content); else node.textContent = content ?? "—"; return node; }
  function action(label, name, id, danger = false) { const b = el("button", `action${danger ? " danger" : ""}`, label); b.dataset.action = name; b.dataset.id = id; return b; }
  function statusBadge(status, active = true) { const cls = !active ? "badge--off" : status === "published" || status === "active" ? "badge--ok" : "badge--warn"; return el("span", `badge ${cls}`, !active ? "موقوف" : (statusNames[status] || "فعّال")); }

  function showView(name) {
    $$(".nav-item").forEach(n => n.classList.toggle("is-active", n.dataset.view === name));
    $$(".view").forEach(n => n.classList.toggle("is-active", n.dataset.panel === name));
    $("#viewTitle").textContent = titles[name]; $("#side").classList.remove("is-open");
    if (name === "audit") loadAudit();
  }
  $$(".nav-item").forEach(n => n.addEventListener("click", () => showView(n.dataset.view)));
  $$('[data-goto]').forEach(n => n.addEventListener("click", () => showView(n.dataset.goto)));
  $("#menu").addEventListener("click", () => $("#side").classList.toggle("is-open"));
  $("#logout").addEventListener("click", () => endSession());

  function openModal(id) { const m = $("#" + id); m.classList.add("is-open"); m.setAttribute("aria-hidden", "false"); }
  function closeModal(m) { m.classList.remove("is-open"); m.setAttribute("aria-hidden", "true"); }
  $$('[data-open]').forEach(n => n.addEventListener("click", () => openModal(n.dataset.open)));
  $$('[data-close]').forEach(n => n.addEventListener("click", () => closeModal(n.closest(".modal"))));
  $$(".modal").forEach(m => m.addEventListener("click", e => { if (e.target === m) closeModal(m); }));

  async function loadIdentity() {
    const me = await api("/api/auth/me");
    $("#operatorName").textContent = me.profile?.name || "الأدمن";
    $("#operatorId").textContent = me.user_id || me.student_id;
  }
  async function loadFiles() { const data = await api("/api/admin/files"); state.files = data.files || []; renderFiles(); renderOverview(); }
  async function loadEmployees() { const data = await api("/api/admin/employees"); state.employees = data.employees || []; renderEmployees(); renderOverview(); }
  async function loadAudit() { try { const data = await api("/api/admin/audit?limit=100"); state.audit = data.events || []; renderAudit(); renderOverview(); } catch (e) { toast(e.message, true); } }

  function fileTitle(file) { const wrap = el("div", "cell-title"); wrap.append(el("span", "file-icon", "JSON")); const info = el("div"); info.append(el("strong", "", file.name || file.collection), el("small", "", file.collection)); wrap.append(info); return wrap; }
  function rolesList(roles = []) { const wrap = el("div", "roles"); roles.forEach(role => wrap.append(el("span", "role-dot", roleNames[role] || role))); return wrap; }
  function renderFiles() {
    const q = $("#fileSearch").value.trim().toLowerCase(); const body = $("#filesBody"); body.replaceChildren();
    const files = state.files.filter(f => !q || (f.name || f.collection || "").toLowerCase().includes(q));
    if (!files.length) { const tr = el("tr"); const td = el("td", "empty", "لا توجد ملفات مطابقة."); td.colSpan = 6; tr.append(td); body.append(tr); return; }
    files.forEach(file => {
      const tr = el("tr"); const acts = el("div", "actions"); const id = file.file_id;
      const unresolved = Number(file.preflight?.unresolved_conflict_count || 0);
      // كل ملف — حتى القديم السابق للسجل — له إجراءات: الخادم يتبنّاه تلقائياً.
      if (file.status === "draft") acts.append(action("معالجة ونشر", "process_publish", id));
      if (file.status === "ready" && unresolved) {
        acts.append(action("الاحتفاظ بالموجود", "keep_existing", id));
        acts.append(action("اعتماد الجديد", "prefer_incoming", id));
      } else if (file.status === "ready") acts.append(action("نشر", "publish", id));
      if (file.status !== "archived") {
        acts.append(action("الصلاحيات", "access", id));
        acts.append(action("حذف", "delete", id, true));
      }
      tr.append(cell(fileTitle(file)), cell(el("span", "badge", classNames[file.classification] || file.classification)), cell(`v${file.published_version || file.latest_version || 1}`), cell(statusBadge(file.status)), cell(rolesList(file.allowed_roles)), cell(acts)); body.append(tr);
    });
  }
  function renderEmployees() {
    const q = $("#employeeSearch").value.trim().toLowerCase(); const body = $("#employeesBody"); body.replaceChildren();
    const items = state.employees.filter(x => !q || `${x.profile?.name || ""} ${x.profile?.department || ""} ${x.user_id}`.toLowerCase().includes(q));
    if (!items.length) { const tr = el("tr"); const td = el("td", "empty", "لا توجد حسابات مطابقة."); td.colSpan = 6; tr.append(td); body.append(tr); return; }
    items.forEach(item => { const tr = el("tr"); const title = el("div", "cell-title"); title.append(el("span", "file-icon", (item.profile?.name || "م")[0])); const info = el("div"); info.append(el("strong", "", item.profile?.name || "موظف"), el("small", "", item.user_id)); title.append(info); const acts = el("div", "actions"); acts.append(action(item.active ? "تعطيل" : "تفعيل", "toggle", item.user_id, item.active), action("إنهاء الجلسات", "sessions", item.user_id), action("إعادة كلمة المرور", "password", item.user_id)); tr.append(cell(title), cell(item.profile?.department), cell(item.profile?.job_title), cell(statusBadge("active", item.active)), cell(rolesList(item.access_groups || [])), cell(acts)); body.append(tr); });
  }
  function renderAudit() { const body = $("#auditBody"); body.replaceChildren(); if (!state.audit.length) { const tr = el("tr"); const td = el("td", "empty", "لا توجد أحداث بعد."); td.colSpan = 5; tr.append(td); body.append(tr); return; } state.audit.forEach(event => { const time = event.created_at ? new Date(event.created_at).toLocaleString("ar-EG") : "—"; const details = event.details && Object.keys(event.details).length ? JSON.stringify(event.details) : "—"; const tr = el("tr"); tr.append(cell(time), cell(event.actor_id), cell(el("span", "badge", event.action)), cell(event.target), cell(details)); body.append(tr); }); }
  function renderOverview() { $("#statFiles").textContent = state.files.length; $("#statPublished").textContent = state.files.filter(f => f.status === "published").length; $("#statEmployees").textContent = state.employees.length; $("#statAudit").textContent = state.audit.length; const root = $("#recentFiles"); root.replaceChildren(); state.files.slice(0,5).forEach(file => { const item = el("div", "activity__item"); item.append(el("span", "activity__icon", "▤")); const info = el("div"); info.append(el("strong", "", file.name || file.collection), el("small", "", `${classNames[file.classification] || file.classification} · ${statusNames[file.status] || file.status}`)); item.append(info, el("time", "", file.updated_at ? new Date(file.updated_at).toLocaleDateString("ar-EG") : "قديم")); root.append(item); }); if (!root.children.length) root.append(el("div", "empty", "ابدأ برفع أول مسودة معرفة.")); }

  $("#fileSearch").addEventListener("input", renderFiles); $("#employeeSearch").addEventListener("input", renderEmployees); $("#refreshAudit").addEventListener("click", loadAudit);
  $("#adoptAll").addEventListener("click", async e => {
    const b = e.currentTarget; b.disabled = true;
    try { const r = await api("/api/admin/files/adopt-all", { method: "POST" });
      toast(r.message); await loadFiles(); await loadAudit();
    } catch (err) { toast(err.message, true); } finally { b.disabled = false; }
  });
  let accessTarget = null;   // الملف الذي يُحرَّر في accessModal
  function openAccessModal(file) {
    accessTarget = file;
    const form = $("#accessForm");
    $("#accessTitle").textContent = `صلاحيات: ${file.name || file.collection}`;
    form.elements.classification.value = file.classification || "university_public";
    const roles = file.allowed_roles || [];
    $$("input[name=roles]", form).forEach(box => { box.checked = roles.includes(box.value); });
    form.elements.owner_id.value = file.owner_id || "";
    $("[data-msg]", form).textContent = "";
    syncPolicyControls(form);
    openModal("accessModal");
  }

  $("#filesBody").addEventListener("click", async e => {
    const b = e.target.closest("[data-action]"); if (!b) return;
    const file = state.files.find(x => x.file_id === b.dataset.id); if (!file) return;
    if (b.dataset.action === "access") { openAccessModal(file); return; }
    if (b.dataset.action === "delete" &&
        !confirm(`سيُحذف «${file.name || file.collection}» نهائياً من المعرفة والبحث.\nهل أنت متأكد؟`)) return;
    b.disabled = true;
    try {
      if (b.dataset.action === "process_publish") {
        const item = await api(`/api/admin/files/${file.file_id}/process`, { method:"POST" });
        if (Number(item.preflight?.unresolved_conflict_count || 0)) {
          toast("اكتملت المعالجة، لكن النشر متوقف حتى تختار قرار التعارض.", true);
        } else {
          await api(`/api/admin/files/${item.file_id || file.file_id}/publish`, { method:"POST" });
          toast("تمت المعالجة والنشر — الملف أصبح متاحاً حسب صلاحياته.");
        }
      }
      if (["keep_existing", "prefer_incoming"].includes(b.dataset.action)) {
        await api(`/api/admin/files/${file.file_id}/resolve-conflicts`, {
          method:"POST", body:JSON.stringify({ decision:b.dataset.action, conflict_ids:[] })
        });
        toast(b.dataset.action === "keep_existing" ? "سُجل قرار الاحتفاظ بالموجود؛ يمكنك النشر الآن." : "سُجل قرار اعتماد الجديد؛ يمكنك النشر الآن.");
      }
      if (b.dataset.action === "publish") { await api(`/api/admin/files/${file.file_id}/publish`, { method:"POST" }); toast("تم النشر."); }
      if (b.dataset.action === "delete") { const r = await api(`/api/admin/files/${encodeURIComponent(file.file_id)}`, { method:"DELETE" }); toast(r.message || "تم الحذف."); }
      await loadFiles(); await loadAudit();
    } catch (err) { toast(err.message, true); } finally { b.disabled = false; }
  });

  $("#accessForm").addEventListener("submit", async e => {
    e.preventDefault();
    if (!accessTarget) return;
    const form = e.currentTarget; const msg = $("[data-msg]", form);
    const button = $("button[type=submit]", form); msg.textContent = "";
    const roles = $$("input[name=roles]:checked", form).map(x => x.value);
    if (!roles.length) { msg.textContent = "اختر دوراً واحداً على الأقل."; return; }
    button.disabled = true;
    try {
      await api(`/api/admin/files/${encodeURIComponent(accessTarget.file_id)}/access`, {
        method: "PATCH",
        body: JSON.stringify({
          classification: form.elements.classification.value,
          allowed_roles: roles,
          owner_id: form.elements.owner_id.value.trim() || null,
        }),
      });
      closeModal($("#accessModal"));
      toast("حُدّثت الصلاحيات وسرَت فوراً على البحث.");
      await loadFiles(); await loadAudit();
    } catch (err) { msg.textContent = err.message; } finally { button.disabled = false; }
  });
  $("#accessForm").elements.classification.addEventListener("change", e => syncPolicyControls(e.target.form));
  $("#employeesBody").addEventListener("click", async e => {
    const b = e.target.closest("[data-action]");
    if (!b) return;
    const item = state.employees.find(x => x.user_id === b.dataset.id);
    if (!item) return;
    let payload;
    if (b.dataset.action === "toggle") payload = { active:!item.active };
    if (b.dataset.action === "sessions") payload = { end_sessions:true };
    if (b.dataset.action === "password") {
      const value = prompt("كلمة المرور المؤقتة الجديدة - 8 أحرف على الأقل");
      if (!value) return;
      payload = { temporary_password:value, end_sessions:true };
    }
    b.disabled = true;
    try {
      await api(`/api/admin/employees/${item.user_id}`, {
        method:"PATCH", body:JSON.stringify(payload),
      });
      toast("تم تحديث حساب الموظف.");
      await loadEmployees();
      await loadAudit();
    } catch (err) {
      toast(err.message, true);
    } finally {
      b.disabled = false;
    }
  });

  $("#fileForm").addEventListener("submit", async e => {
    e.preventDefault();
    const form = e.currentTarget; const f = form.elements;
    const msg = $("[data-msg]", form); const button = $("button[type=submit]", form);
    msg.textContent = "";
    let documents;
    try { documents = JSON.parse(f.documents.value); }
    catch (_) { msg.textContent = "صيغة JSON غير صالحة."; return; }
    const roles = $$('input[name=roles]:checked', form).map(x => x.value);
    if (!roles.length) { msg.textContent = "اختر دوراً واحداً على الأقل."; return; }
    const publishNow = f.publish_now.checked;
    let conflictDecision = null;
    button.disabled = true;
    button.textContent = publishNow ? "جارٍ الرفع والنشر…" : "جارٍ الحفظ…";
    try {
      const preflight = await api("/api/admin/files/preflight", { method:"POST", body:JSON.stringify({
        collection: f.collection.value.trim(), documents,
      }) });
      if (Number(preflight.conflict_count || 0)) {
        const choice = prompt(
          `وُجد ${preflight.conflict_count} تعارضاً. اكتب «الموجود» للاحتفاظ بالبيانات الحالية، أو «الجديد» لاعتماد الملف الوارد. اتركه فارغاً لإلغاء الرفع.`
        );
        if (!choice) return;
        if (choice.trim() === "الموجود") conflictDecision = "keep_existing";
        else if (choice.trim() === "الجديد") conflictDecision = "prefer_incoming";
        else { msg.textContent = "القرار غير معروف؛ اكتب الموجود أو الجديد."; return; }
      }
      const item = await api("/api/admin/files", { method:"POST", body:JSON.stringify({
        collection: f.collection.value.trim(),
        documents,
        classification: f.classification.value,
        allowed_roles: roles,
        owner_id: f.owner_id.value.trim() || null,
      }) });
      if (publishNow) {
        // معالجة + نشر متتاليان: تُبنى المقاطع والفهرس وتسري الصلاحيات فوراً.
        await api(`/api/admin/files/${item.file_id}/process`, { method:"POST" });
        if (conflictDecision) await api(`/api/admin/files/${item.file_id}/resolve-conflicts`, {
          method:"POST", body:JSON.stringify({ decision:conflictDecision, conflict_ids:[] })
        });
        await api(`/api/admin/files/${item.file_id}/publish`, { method:"POST" });
        toast("رُفع الملف ونُشر — صار ضمن معرفة البوت حسب صلاحياته.");
      } else {
        toast("حُفظت المسودة. عالجها ثم انشرها من الجدول.");
      }
      form.reset(); syncPolicyControls(form);
      closeModal($("#fileModal"));
      await loadFiles(); await loadAudit(); showView("files");
    } catch (err) { msg.textContent = err.message; }
    finally { button.disabled = false; button.textContent = "حفظ"; }
  });
  $("#fileForm").elements.classification.addEventListener("change", e => syncPolicyControls(e.target.form));
  syncPolicyControls($("#fileForm"));
  $("#employeeForm").addEventListener("submit", async e => { e.preventDefault(); const form = e.currentTarget; const f = form.elements; const msg = $("[data-msg]", form); const button = $("button[type=submit]", form); msg.textContent = ""; const body = { employee_id:f.employee_id.value.trim(), temporary_password:f.temporary_password.value, name:f.name.value.trim(), department:f.department.value.trim(), job_title:f.job_title.value.trim(), salary:f.salary.value ? Number(f.salary.value) : null, access_groups:f.access_groups.value.split(",").map(x=>x.trim()).filter(Boolean) }; button.disabled = true; try { await api("/api/admin/employees", { method:"POST", body:JSON.stringify(body) }); form.reset(); closeModal($("#employeeModal")); toast("تم إنشاء حساب الموظف بكلمة مرور مؤقتة."); await loadEmployees(); await loadAudit(); showView("employees"); } catch (err) { msg.textContent = err.message; } finally { button.disabled = false; } });

  Promise.all([loadIdentity(), loadFiles(), loadEmployees(), loadAudit()]).catch(err => toast(err.message, true));
})();
