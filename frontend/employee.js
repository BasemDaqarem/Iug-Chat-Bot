(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

  let session;
  try {
    session = JSON.parse(sessionStorage.getItem("iug_auth") || "null");
  } catch (_) {}

  if (!session || !session.token || session.role !== "employee") {
    location.replace("index.html");
    return;
  }

  const titles = {
    home: "الرئيسية",
    students: "دليل الطلاب",
    profile: "ملفي الوظيفي",
  };

  async function api(path, options = {}) {
    const headers = {
      ...(options.headers || {}),
      Authorization: `Bearer ${session.token}`,
    };
    if (options.body) headers["Content-Type"] = "application/json";
    const response = await fetch(path, { ...options, headers });
    let data = null;
    try { data = await response.json(); } catch (_) {}
    if (response.status === 401) {
      logout("انتهت صلاحية الجلسة.");
      throw new Error("unauthorized");
    }
    if (!response.ok) {
      throw new Error(data?.error?.message || "تعذّر إتمام الطلب.");
    }
    return data;
  }

  function logout(message = "") {
    sessionStorage.removeItem("iug_auth");
    if (message) sessionStorage.setItem("iug_flash", message);
    location.replace("index.html");
  }

  let toastTimer;
  function toast(message, error = false) {
    const target = $("#toast");
    target.textContent = message;
    target.className = `toast show${error ? " error" : ""}`;
    target.setAttribute("aria-live", error ? "assertive" : "polite");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { target.className = "toast"; }, 3000);
  }

  const side = $("#side");
  const menu = $("#menu");
  const sideBackdrop = $("#sideBackdrop");

  function setMenu(open) {
    side.classList.toggle("is-open", open);
    sideBackdrop.classList.toggle("is-open", open);
    menu.setAttribute("aria-expanded", String(open));
    sideBackdrop.tabIndex = open ? 0 : -1;
  }

  function showView(name) {
    $$(".nav-item").forEach((item) => {
      const active = item.dataset.view === name;
      item.classList.toggle("is-active", active);
      if (active) item.setAttribute("aria-current", "page");
      else item.removeAttribute("aria-current");
    });
    $$(".view").forEach((panel) => {
      panel.classList.toggle("is-active", panel.dataset.panel === name);
    });
    $("#viewTitle").textContent = titles[name];
    setMenu(false);
    if (name === "students") loadStudents();
  }

  $$(".nav-item").forEach((item) => {
    item.addEventListener("click", () => showView(item.dataset.view));
  });
  $$("[data-goto]").forEach((item) => {
    item.addEventListener("click", () => showView(item.dataset.goto));
  });
  menu.addEventListener("click", () => setMenu(!side.classList.contains("is-open")));
  sideBackdrop.addEventListener("click", () => setMenu(false));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && side.classList.contains("is-open")) {
      setMenu(false);
      menu.focus();
    }
  });
  $("#logout").addEventListener("click", () => logout());

  function setText(selector, value) {
    $(selector).textContent = value ?? "—";
  }

  async function loadProfile() {
    const me = await api("/api/portal/me");
    const profile = me.profile || {};
    const name = profile.name || "الموظف";
    const initial = name.trim()[0] || "م";

    setText("#operatorName", name);
    setText("#operatorId", me.user_id);
    setText("#avatar", initial);
    setText("#welcomeName", `مرحباً، ${name.split(" ")[0]}`);
    setText("#homeDepartment", profile.department);
    setText("#homeTitle", profile.job_title);
    setText("#profileName", name);
    setText("#profileId", me.user_id);
    setText("#profileAvatar", initial);
    setText("#profileDepartment", profile.department);
    setText("#profileTitle", profile.job_title);
    setText(
      "#profileUpdated",
      profile.updated_at ? new Date(profile.updated_at).toLocaleDateString("ar-EG") : "—",
    );
    setText(
      "#profileSalary",
      profile.salary == null
        ? "غير مسجل"
        : `${new Intl.NumberFormat("ar-JO", { maximumFractionDigits: 2 }).format(profile.salary)} دينار أردني`,
    );
  }

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text != null) element.textContent = text;
    return element;
  }

  function renderTableMessage(text) {
    const row = node("tr");
    const cell = node("td", "empty", text);
    cell.colSpan = 6;
    row.append(cell);
    return row;
  }

  async function loadStudents() {
    const body = $("#studentsBody");
    body.replaceChildren(renderTableMessage("جارٍ تحميل الدليل…"));
    body.setAttribute("aria-busy", "true");

    try {
      const query = encodeURIComponent($("#studentQuery").value.trim());
      const data = await api(`/api/portal/students?query=${query}&limit=50`);
      body.replaceChildren();
      if (!data.students?.length) {
        body.append(renderTableMessage("لا توجد نتائج مطابقة."));
        return;
      }

      data.students.forEach((student) => {
        const row = node("tr");
        const title = node("div", "student-name");
        title.append(
          node("strong", "", student.name || "طالب"),
          node("small", "", student.student_id),
        );
        const values = [
          title,
          student.major,
          student.gpa ?? "—",
          student.rank ?? "—",
          student.academic_status || "—",
          student.updated_at
            ? new Date(student.updated_at).toLocaleDateString("ar-EG")
            : "—",
        ];
        values.forEach((value) => {
          const cell = node("td");
          if (value instanceof Node) cell.append(value);
          else cell.textContent = value;
          row.append(cell);
        });
        body.append(row);
      });
    } catch (error) {
      body.replaceChildren(renderTableMessage("تعذّر تحميل دليل الطلاب."));
      toast(error.message, true);
    } finally {
      body.removeAttribute("aria-busy");
    }
  }

  $("#studentSearch").addEventListener("click", loadStudents);
  $("#studentQuery").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      loadStudents();
    }
  });

  function message(text, author) {
    const item = node("div", `emp-msg emp-msg--${author}`, text);
    $("#messages").append(item);
    $("#messages").scrollTop = $("#messages").scrollHeight;
    return item;
  }

  let busy = false;
  async function ask(rawQuestion) {
    const question = rawQuestion.trim();
    if (!question || busy) return;

    busy = true;
    const input = $("#question");
    const button = $("#chatForm button");
    button.disabled = true;
    message(question, "me");
    input.value = "";
    const pending = message("يفكر المساعد…", "bot");

    try {
      const data = await api("/api/chat", {
        method: "POST",
        body: JSON.stringify({ question }),
      });
      pending.textContent = data.answer || "لا تتوفر إجابة.";
    } catch (error) {
      pending.className = "emp-msg emp-msg--error";
      pending.textContent = `تنبيه: ${error.message}`;
    } finally {
      busy = false;
      button.disabled = false;
      input.focus();
    }
  }

  $("#chatForm").addEventListener("submit", (event) => {
    event.preventDefault();
    ask($("#question").value);
  });

  $$(".employee-chips button").forEach((button) => {
    button.addEventListener("click", () => {
      const fill = button.dataset.fill;
      if (fill) {
        const input = $("#question");
        input.value = fill;
        input.focus();
        input.setSelectionRange(fill.length, fill.length);
      } else {
        ask(button.textContent);
      }
    });
  });

  loadProfile().catch((error) => toast(error.message, true));
})();
