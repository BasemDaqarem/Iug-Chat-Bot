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
      if (!file.legacy && file.status === "draft") acts.append(action("معالجة", "process", id));
      if (!file.legacy && file.status === "ready") acts.append(action("نشر", "publish", id));
      if (!file.legacy) acts.append(action("الصلاحيات", "access", id));
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
  $("#filesBody").addEventListener("click", async e => { const b = e.target.closest("[data-action]"); if (!b) return; const file = state.files.find(x => x.file_id === b.dataset.id); if (!file) return; b.disabled = true; try { if (b.dataset.action === "process") await api(`/api/admin/files/${file.file_id}/process`, { method:"POST" }); if (b.dataset.action === "publish") await api(`/api/admin/files/${file.file_id}/publish`, { method:"POST" }); if (b.dataset.action === "access") { const classification = prompt("التصنيف", file.classification); if (!classification) return; const roles = prompt("الأدوار مفصولة بفاصلة", (file.allowed_roles || []).join(",")); if (!roles) return; await api(`/api/admin/files/${file.file_id}/access`, { method:"PATCH", body:JSON.stringify({ classification, allowed_roles:roles.split(",").map(x=>x.trim()).filter(Boolean), owner_id:file.owner_id || null }) }); } toast("تم تحديث الملف بنجاح."); await loadFiles(); await loadAudit(); } catch (err) { toast(err.message, true); } finally { b.disabled = false; } });
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

  $("#fileForm").addEventListener("submit", async e => { e.preventDefault(); const form = e.currentTarget; const f = form.elements; const msg = $("[data-msg]", form); const button = $("button[type=submit]", form); msg.textContent = ""; let documents; try { documents = JSON.parse(f.documents.value); } catch (_) { msg.textContent = "صيغة JSON غير صالحة."; return; } const roles = $$('input[name=roles]:checked', form).map(x => x.value); button.disabled = true; try { await api("/api/admin/files", { method:"POST", body:JSON.stringify({ collection:f.collection.value.trim(), documents, classification:f.classification.value, allowed_roles:roles }) }); form.reset(); closeModal($("#fileModal")); toast("حُفظت المسودة. عالجها ثم انشرها من الجدول."); await loadFiles(); await loadAudit(); showView("files"); } catch (err) { msg.textContent = err.message; } finally { button.disabled = false; } });
  $("#employeeForm").addEventListener("submit", async e => { e.preventDefault(); const form = e.currentTarget; const f = form.elements; const msg = $("[data-msg]", form); const button = $("button[type=submit]", form); msg.textContent = ""; const body = { employee_id:f.employee_id.value.trim(), temporary_password:f.temporary_password.value, name:f.name.value.trim(), department:f.department.value.trim(), job_title:f.job_title.value.trim(), salary:f.salary.value ? Number(f.salary.value) : null, access_groups:f.access_groups.value.split(",").map(x=>x.trim()).filter(Boolean) }; button.disabled = true; try { await api("/api/admin/employees", { method:"POST", body:JSON.stringify(body) }); form.reset(); closeModal($("#employeeModal")); toast("تم إنشاء حساب الموظف بكلمة مرور مؤقتة."); await loadEmployees(); await loadAudit(); showView("employees"); } catch (err) { msg.textContent = err.message; } finally { button.disabled = false; } });

  Promise.all([loadIdentity(), loadFiles(), loadEmployees(), loadAudit()]).catch(err => toast(err.message, true));
})();
