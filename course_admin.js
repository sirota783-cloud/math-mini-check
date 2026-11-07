
(() => {
  "use strict";
  const BASE = "https://math-mini-check.onrender.com";
  let courses = [];
  let preview = null;
  const $ = id => document.getElementById(id);

  function make(tag, label, style) {
    const el = document.createElement(tag);
    if (label !== undefined) el.textContent = label;
    if (style) Object.assign(el.style, style);
    return el;
  }

  function option(value, label) {
    const el = make("option", label);
    el.value = value;
    return el;
  }

  function mark(el, message, error = false) {
    el.textContent = message;
    el.style.color = error ? "#b63e45" : "#278645";
  }

  async function api(path, body) {
    const response = await fetch(BASE + path, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body)
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.error || "בקשה נכשלה");
    return result;
  }

  function teacherPassword() {
    return $("teacher_password")?.value?.trim() || "";
  }

  function applyCourseList() {
    const studentSelect = $("course_select");
    const teacherSelect = $("course_filter");

    for (const sel of [studentSelect, teacherSelect]) {
      if (!sel) continue;
      const chosen = sel.value;
      const oldOptions = Array.from(sel.options)
        .map(x => [x.value, x.textContent]);

      sel.replaceChildren(
        ...courses.map(c => option(c.course_id, c.display_name))
      );

      if (teacherSelect === sel)
        sel.append(option("ALL", "כל המקצועות"));

      if (chosen && !Array.from(sel.options)
          .some(x => x.value === chosen)) {
        const prior = oldOptions.find(x => x[0] === chosen);
        sel.append(option(chosen, prior?.[1] || chosen));
      }

      if (chosen) sel.value = chosen;
    }

    try {
      if (typeof COURSE_LABELS === "object" &&
          COURSE_LABELS !== null) {
        courses.forEach(c => {
          COURSE_LABELS[c.course_id] = c.display_name;
        });
      }
    } catch (_) {}
  }

  function setCourses(newCourses) {
    courses = newCourses.filter(c =>
      /^[A-Z][A-Z0-9_-]{1,23}$/.test(c.course_id)
    );
    applyCourseList();

    const selector = $("delete_course_select");
    if (selector) {
      const previous = selector.value;
      selector.replaceChildren(
        ...courses.map(c => option(c.course_id, c.display_name))
      );
      if (courses.some(c => c.course_id === previous))
        selector.value = previous;
    }
  }

  async function loadCourses() {
    const response = await fetch(BASE + "/courses");
    if (!response.ok) throw new Error("לא ניתן לטעון קורסים");
    const result = await response.json();
    if (!Array.isArray(result.courses))
      throw new Error("רשימת הקורסים אינה זמינה");
    setCourses(result.courses);
  }

  function insertAdminTools() {
    const target = $("teacher_controls");
    if (!target) return;

    const panel = make("section", undefined, {
      background: "#fffefa",
      padding: "22px",
      marginTop: "20px",
      border: "1px solid #d7e5ec",
      borderRadius: "16px"
    });

    panel.append(make("h3", "ניהול קורסים ומחיקת עבודות"));

    const code = make("input");
    code.placeholder = "קוד קורס באנגלית, למשל ALGEBRA4";

    const name = make("input");
    name.placeholder = "שם הקורס בעברית";

    const create = make("button", "יצירת קורס חדש");
    const createStatus = make("p");

    const wrap = make("div", undefined, {
      display: "grid",
      gridTemplateColumns: "1fr 1fr",
      gap: "10px",
      margin: "10px 0"
    });

    wrap.append(code, name);

    panel.append(
      make("h4", "יצירת קורס חדש"),
      wrap,
      create,
      createStatus
    );

    panel.append(
      make("hr"),
      make("h4", "מחיקת עבודות בקורס")
    );

    const selectCourse = make("select");
    const selectQuiz = make("select");

    selectCourse.id = "delete_course_select";
    selectQuiz.append(option("ALL", "כל התרגולים בקורס"));

    for (let i = 1; i <= 30; i++)
      selectQuiz.append(option(String(i), "תרגול " + i));

    const picker = make("div", undefined, {
      display: "grid",
      gridTemplateColumns: "1fr 1fr",
      gap: "10px",
      margin: "12px 0"
    });

    picker.append(selectCourse, selectQuiz);

    const previewButton = make(
      "button", "בדיקת מספר העבודות למחיקה"
    );

    const deleteButton = make("button", "מחיקת עבודות", {
      marginTop: "8px",
      background: "#b63e45"
    });

    deleteButton.disabled = true;

    const deleteStatus = make("p");

    panel.append(
      picker, previewButton, deleteButton, deleteStatus
    );

    target.append(panel);

    create.onclick = async () => {
      const id = code.value.trim().toUpperCase();
      const label = name.value.trim();

      if (!/^[A-Z][A-Z0-9_-]{1,23}$/.test(id) ||
          label.length < 4) {
        mark(createStatus, "יש להזין קוד ושם קורס תקינים", true);
        return;
      }

      create.disabled = true;

      try {
        await api("/teacher/courses/manage", {
          teacher_password: teacherPassword(),
          course_id: id,
          display_name: label
        });

        await loadCourses();
        mark(createStatus, "הקורס נוצר בהצלחה");
        code.value = "";
        name.value = "";
      } catch (error) {
        mark(createStatus, "שגיאה: " + error.message, true);
      } finally {
        create.disabled = false;
      }
    };

    function invalidatePreview() {
      preview = null;
      deleteButton.disabled = true;
      mark(deleteStatus, "");
    }

    selectCourse.onchange = invalidatePreview;
    selectQuiz.onchange = invalidatePreview;

    previewButton.onclick = async () => {
      previewButton.disabled = true;

      try {
        const course_id = selectCourse.value;
        const quiz_id = selectQuiz.value;

        const result = await api(
          "/teacher/submissions/manage", {
            action: "preview",
            teacher_password: teacherPassword(),
            course_id,
            quiz_id
          }
        );

        preview = {...result, course_id, quiz_id};
        deleteButton.disabled = result.count === 0;

        mark(deleteStatus,
          "נמצאו " + result.count +
          " עבודות למחיקה. נתוני התלמידים נשמרים."
        );
      } catch (error) {
        invalidatePreview();
        mark(deleteStatus, error.message, true);
      } finally {
        previewButton.disabled = false;
      }
    };

    deleteButton.onclick = async () => {
      if (!preview || preview.count === 0) return;

      const target = preview.course_id + "/" + preview.quiz_id;

      if (!window.confirm(
        "למחוק לצמיתות " + preview.count +
        " עבודות עבור " + target + "?"
      )) return;

      const typed = window.prompt(
        "לאישור המחיקה הקלידו: DELETE " + target,
        ""
      );

      if (typed !== "DELETE " + target) {
        mark(deleteStatus, "המחיקה בוטלה", true);
        return;
      }

      deleteButton.disabled = true;

      try {
        const result = await api(
          "/teacher/submissions/manage", {
            action: "delete",
            teacher_password: teacherPassword(),
            course_id: preview.course_id,
            quiz_id: preview.quiz_id,
            expected_count: preview.count,
            confirmation: "DELETE " + target
          }
        );

        mark(deleteStatus,
          "נמחקו " + result.deleted + " עבודות. " +
          (result.warnings || []).join("; ")
        );
        preview = null;
      } catch (error) {
        mark(deleteStatus, "שגיאה: " + error.message, true);
        preview = null;
      }
    };
  }

  insertAdminTools();
  loadCourses().catch(console.error);
})();
