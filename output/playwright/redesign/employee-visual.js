async (page) => {
  await page.route("**/api/**", async (route) => {
    const requestUrl = route.request().url();
    let body = {};

    if (requestUrl.includes("/api/portal/me")) {
      body = {
        user_id: "EMP-1001",
        role: "employee",
        profile: {
          name: "أحمد الموظف",
          department: "القبول والتسجيل",
          job_title: "مسجل أكاديمي",
          salary: 1250,
          updated_at: "2026-07-22T09:30:00Z"
        }
      };
    } else if (requestUrl.includes("/api/portal/students")) {
      body = {
        students: [
          {
            student_id: "120210001",
            name: "سارة محمد",
            major: "هندسة الحاسوب",
            gpa: 89.4,
            rank: 4,
            academic_status: "منتظم",
            updated_at: "2026-07-22T08:00:00Z"
          },
          {
            student_id: "120210018",
            name: "ليان أحمد",
            major: "نظم المعلومات",
            gpa: 84.7,
            rank: 11,
            academic_status: "منتظم",
            updated_at: "2026-07-21T13:45:00Z"
          }
        ],
        count: 2
      };
    } else if (requestUrl.includes("/api/chat")) {
      body = {
        answer: "هذه إجابة تجريبية للتحقق البصري فقط.",
        sources: []
      };
    }

    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(body)
    });
  });

  await page.addInitScript(() => {
    sessionStorage.setItem("iug_session", JSON.stringify({
      token: "visual-check-token",
      role: "employee",
      user_id: "EMP-1001"
    }));
  });

  await page.goto("http://127.0.0.1:8000/app/employee.html");
  await page.waitForTimeout(700);
}
