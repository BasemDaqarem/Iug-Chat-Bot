async (page) => {
  await page.route("**/api/**", async (route) => {
    const requestUrl = route.request().url();
    let payload = {};

    if (requestUrl.includes("/api/auth/me")) {
      payload = {
        user_id: "ADMIN-01",
        role: "admin",
        profile: { name: "مدير النظام" },
      };
    } else if (requestUrl.includes("/api/admin/files")) {
      payload = {
        files: [
          {
            file_id: "f1",
            name: "دليل القبول والتسجيل",
            collection: "admissions_2026",
            classification: "university_public",
            status: "published",
            published_version: 3,
            allowed_roles: ["guest", "student", "employee", "admin"],
            updated_at: "2026-07-22T10:00:00Z",
          },
          {
            file_id: "f2",
            name: "إجراءات شؤون الموظفين",
            collection: "employee_procedures",
            classification: "employee_internal",
            status: "ready",
            latest_version: 2,
            allowed_roles: ["employee", "admin"],
            updated_at: "2026-07-21T08:30:00Z",
          },
        ],
      };
    } else if (requestUrl.includes("/api/admin/employees")) {
      payload = {
        employees: [
          {
            user_id: "EMP-1001",
            active: true,
            access_groups: ["admissions"],
            profile: {
              name: "أحمد الموظف",
              department: "القبول والتسجيل",
              job_title: "مسجل أكاديمي",
            },
          },
        ],
      };
    } else if (requestUrl.includes("/api/admin/audit")) {
      payload = {
        events: [
          {
            created_at: "2026-07-23T08:00:00Z",
            actor_id: "ADMIN-01",
            action: "file.publish",
            target: "admissions_2026",
            details: { version: 3 },
          },
        ],
      };
    }

    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(payload),
    });
  });

  await page.evaluate(() => {
    sessionStorage.setItem("iug_auth", JSON.stringify({
      user_id: "ADMIN-01",
      student_id: "ADMIN-01",
      name: "مدير النظام",
      role: "admin",
      token: "visual-test-token",
    }));
  });
  await page.goto("http://127.0.0.1:8000/app/admin.html");
  await page.waitForTimeout(700);
}
