import os
import hashlib
import uuid
import json
import base64
import urllib.request
import urllib.error

from flask import Flask, request, jsonify, render_template_string
from flask_cors import CORS
from supabase import create_client


app = Flask(__name__)
CORS(app)

sb = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_SERVICE_KEY"]
)

TEACHER_KEY = os.environ["TEACHER_KEY"]
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5").strip()
AUTO_GRADE_CONFIDENCE = float(os.environ.get("AUTO_GRADE_CONFIDENCE", "0.75"))


def phash(code, pin):
    return hashlib.sha256(
        (code + "|" + pin).encode()
    ).hexdigest()


def teacher_ok():
    return (
        request.headers.get("X-Teacher-Key", "")
        == TEACHER_KEY
    )


def clean_part(value, default):
    value = (value or "").strip()
    return value if value else default


def make_quiz_key(course_id, group_id, quiz_id):
    """
    We keep one DB column (quiz_id) and encode course + group + quiz in it,
    so no Supabase migration is needed.
    """
    return "v2::{}::{}::{}".format(
        clean_part(course_id, "general"),
        clean_part(group_id, "general"),
        clean_part(quiz_id, "1")
    )


def split_quiz_key(value):
    value = str(value or "")
    if value.startswith("v2::"):
        parts = value.split("::", 3)
        if len(parts) == 4:
            return {
                "course_id": parts[1],
                "group_id": parts[2],
                "quiz_id": parts[3],
            }

    # Old submissions remain readable.
    return {
        "course_id": "",
        "group_id": "",
        "quiz_id": value,
    }


def safe_json_from_text(text):
    text = (text or "").strip()

    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()

    start = text.find("{")
    end = text.rfind("}")

    if start == -1 or end == -1 or end < start:
        raise ValueError("Model did not return JSON")

    return json.loads(text[start:end + 1])


def response_output_text(payload):
    # Responses API normally returns output[].content[].text.
    chunks = []

    for item in payload.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                chunks.append(content.get("text", ""))

    return "\n".join(chunks).strip()


def openai_grade(problem, rubric_text, image_bytes, exam_file=None):
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    prompt = f"""
You are grading a student's handwritten mathematics exam.

The official exam/questions file is provided first when available.
The images after it are the student's submitted work.

Problem / exam description:
{problem}

Teacher grading instructions / rubric:
{rubric_text}

IMPORTANT:
Compare the student's work with the official exam questions.
Grade the ENTIRE exam out of 100 according to the rubric.
A missing question or subquestion receives 0 points.
Never rescale the submitted part to 100.
Do not invent answers that are not visible in the student's work.
Accept any mathematically valid solution method.

Return ONLY one JSON object with these keys:
score: numeric score from 0 to 100,
feedback: short feedback for the student in Hebrew,
confidence: number from 0 to 1,
needs_teacher: true or false.

Set needs_teacher=true if handwriting is unclear, pages seem missing,
the correspondence between answers and questions is ambiguous,
or you are not sufficiently confident.
"""

    content = [
        {
            "type": "input_text",
            "text": prompt
        }
    ]

    # Official exam file
    if exam_file:
        exam_data, exam_mime, exam_name = exam_file
        encoded_exam = base64.b64encode(exam_data).decode("ascii")

        if exam_mime == "application/pdf":
            content.append({
                "type": "input_file",
                "filename": exam_name,
                "file_data": f"data:application/pdf;base64,{encoded_exam}"
            })
        else:
            content.append({
                "type": "input_image",
                "image_url": f"data:{exam_mime};base64,{encoded_exam}",
                "detail": "high"
            })

    # Student work
    for data, mime in image_bytes:
        encoded = base64.b64encode(data).decode("ascii")
        content.append({
            "type": "input_image",
            "image_url": f"data:{mime};base64,{encoded}",
            "detail": "high"
        })

    body = json.dumps({
        "model": OPENAI_MODEL,
        "input": [
            {
                "role": "user",
                "content": content
            }
        ]
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
    )

    try:
       with urllib.request.urlopen(req, timeout=90) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        details = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI HTTP {e.code}: {details[:500]}")
    except Exception as e:
        raise RuntimeError(f"OpenAI request failed: {e}")

    text = response_output_text(payload)
    result = safe_json_from_text(text)

    score = result.get("score")
    feedback = str(result.get("feedback", "")).strip()
    confidence = float(result.get("confidence", 0))
    needs_teacher = bool(result.get("needs_teacher", False))

    if score is not None:
        score = float(score)
        if score.is_integer():
            score = int(score)

    return {
        "score": score,
        "feedback": feedback,
        "confidence": confidence,
        "needs_teacher": needs_teacher,
    }
def load_exam_file(quiz_key):
    """
    Loads the official exam file from Supabase Storage.
    Example:
    v2::MATH::general::3  ->  MATH-3
    """

    parts = split_quiz_key(quiz_key)

    course_id = parts.get("course_id", "")
    quiz_id = parts.get("quiz_id", "")

    if not course_id or not quiz_id:
        return None

    exam_code = f"{course_id}-{quiz_id}"

    candidates = [
        (f"exams/{exam_code}/questions.pdf", "application/pdf", "questions.pdf"),
        (f"exams/{exam_code}/questions.png", "image/png", "questions.png"),
        (f"exams/{exam_code}/questions.jpg", "image/jpeg", "questions.jpg"),
        (f"exams/{exam_code}/questions.jpeg", "image/jpeg", "questions.jpeg"),
    ]

       for path, mime, filename in candidates:
        try:
            data = sb.storage.from_("submissions").download(path)
            if data:
                print("EXAM FILE LOADED:", path, "bytes:", len(data), flush=True)
                return (data, mime, filename)
        except Exception as e:
            print("EXAM FILE ERROR:", path, repr(e), flush=True)

    print("EXAM FILE NOT FOUND FOR:", quiz_key, flush=True)
    return None
    
def auto_grade_submission(submission_id, quiz_key, uploaded_paths):
    """
    Returns the final public status and score.
    Any failure sends the work to teacher review instead of losing it.
    """
    try:
        rubric_rows = (
            sb.table("mini_check_rubrics")
            .select("problem,rubric")
            .eq("quiz_id", quiz_key)
            .limit(1)
            .execute()
            .data
        )

        # The official exam PDF/image is enough to enable automatic grading.
        # A teacher rubric, when present, overrides the default instructions.
        exam_file = load_exam_file(quiz_key)

        if rubric_rows:
            rubric_row = rubric_rows[0]
            problem = rubric_row.get("problem", "") or "Use the attached official exam/questions file."
            rubric_text = rubric_row.get("rubric", "").strip()
        else:
            problem = "Use the attached official exam/questions file."
            rubric_text = ""

        if not rubric_text:
            rubric_text = (
                "Grade the entire submitted exam out of 100. "
                "Use the official exam/questions file as the source of the questions. "
                "Give 0 points for questions or subquestions that were not answered. "
                "Deduct points proportionally for mathematical errors and incomplete reasoning. "
                "Accept any mathematically valid solution method. "
                "Do not rescale a partially submitted exam to 100."
            )

        if exam_file is None and not rubric_rows:
            feedback = "העבודה התקבלה. קובץ שאלות הבחינה לא נמצא ולכן נדרשת בדיקת מרצה."
            sb.table("mini_check_submissions").update({
                "status": "needs_teacher",
                "score": None,
                "feedback": feedback,
            }).eq("id", submission_id).execute()

            return {
                "status": "needs_teacher",
                "score": None,
                "feedback": feedback,
            }

        images = []

        for path in uploaded_paths:
            raw = sb.storage.from_("mini-check-files").download(path)

            ext = os.path.splitext(path)[1].lower()
            mime = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
            }.get(ext, "image/jpeg")

            images.append((raw, mime))

        grade = openai_grade(
            problem=problem,
            rubric_text=rubric_text,
            image_bytes=images,
            exam_file=exam_file
        )

        uncertain = (
            grade["needs_teacher"]
            or grade["confidence"] < AUTO_GRADE_CONFIDENCE
            or grade["score"] is None
        )

        if uncertain:
            status = "needs_teacher"
            score = grade["score"]
            feedback = (
                grade["feedback"]
                or "העבודה התקבלה ונדרשת בדיקת מרצה."
            )
        else:
            status = "graded"
            score = grade["score"]
            feedback = grade["feedback"]

        sb.table("mini_check_submissions").update({
            "status": status,
            "score": score,
            "feedback": feedback,
        }).eq("id", submission_id).execute()

        return {
            "status": status,
            "score": score,
            "feedback": feedback,
        }

    except Exception as e:
        print("AUTO GRADE ERROR:", e)

        feedback = "העבודה התקבלה. הבדיקה האוטומטית לא הסתיימה ולכן נדרשת בדיקת מרצה."

        try:
            sb.table("mini_check_submissions").update({
                "status": "needs_teacher",
                "score": None,
                "feedback": feedback,
            }).eq("id", submission_id).execute()
        except Exception as update_error:
            print("AUTO GRADE STATUS UPDATE ERROR:", update_error)

        return {
            "status": "needs_teacher",
            "score": None,
            "feedback": feedback,
        }


# ==========================================================
# SIMPLE TEACHER PAGE
# ==========================================================

TEACHER_PAGE = """
<!doctype html>
<html lang="he" dir="rtl">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>מרצה – בחנים</title>
    <style>
        body {font-family:Arial,sans-serif;max-width:1100px;margin:30px auto;padding:0 20px}
        input,textarea,button,select{font-size:16px;padding:8px;margin:5px 0;box-sizing:border-box}
        input,textarea{width:100%}
        textarea{min-height:90px}
        button{cursor:pointer}
        .box{border:1px solid #ccc;border-radius:10px;padding:16px;margin-bottom:20px}
        table{width:100%;border-collapse:collapse;margin-top:12px}
        th,td{border:1px solid #ddd;padding:7px;text-align:right}
        th{background:#f4f4f4}
        .grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px}
        @media(max-width:700px){.grid{grid-template-columns:1fr}}
    </style>
</head>
<body>

<h1>מרצה – בחנים</h1>

<div class="box">
    <h2>גישה</h2>
    <input id="teacherKey" type="password" placeholder="Teacher key">
    <button onclick="saveKey()">שמור מפתח</button>
</div>

<div class="box">
    <h2>הגדרת בוחן ובדיקה אוטומטית</h2>

    <div class="grid">
        <div>
            <label>מקצוע</label>
            <input id="courseId" placeholder="לדוגמה: בדידה">
        </div>
        <div>
            <label>קבוצה</label>
            <input id="groupId" placeholder="לדוגמה: A">
        </div>
        <div>
            <label>מספר בוחן</label>
            <input id="quizId" placeholder="לדוגמה: 1">
        </div>
    </div>

    <label>שאלה</label>
    <textarea id="problem"></textarea>

    <label>הוראות בדיקה / מחוון</label>
    <textarea id="rubric"></textarea>

    <button onclick="saveProblem()">שמור</button>
    <div id="saveStatus"></div>
</div>

<div class="box">
    <h2>הגשות</h2>
    <button onclick="loadSubmissions()">רענן</button>
    <div id="loadStatus"></div>

    <table>
        <thead>
            <tr>
                <th>סטודנט</th>
                <th>מקצוע</th>
                <th>קבוצה</th>
                <th>בוחן</th>
                <th>ציון</th>
                <th>סטטוס</th>
                <th>תאריך</th>
            </tr>
        </thead>
        <tbody id="submissionRows"></tbody>
    </table>
</div>

<script>
function getKey() {
    return localStorage.getItem("teacher_key") || "";
}

function saveKey() {
    const key = document.getElementById("teacherKey").value.trim();
    if (!key) return alert("יש להזין מפתח");
    localStorage.setItem("teacher_key", key);
    alert("נשמר");
}

window.addEventListener("load", () => {
    document.getElementById("teacherKey").value = getKey();
});

async function saveProblem() {
    const key = getKey();
    if (!key) return alert("יש לשמור קודם מפתח מרצה");

    const payload = {
        course_id: document.getElementById("courseId").value.trim(),
        group_id: document.getElementById("groupId").value.trim(),
        quiz_id: document.getElementById("quizId").value.trim(),
        problem: document.getElementById("problem").value.trim(),
        rubric: document.getElementById("rubric").value.trim()
    };

    if (!payload.quiz_id) return alert("חסר מספר בוחן");

    const status = document.getElementById("saveStatus");
    status.textContent = "שומר...";

    const response = await fetch("/teacher/rubric", {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            "X-Teacher-Key": key
        },
        body: JSON.stringify(payload)
    });

    status.textContent = response.ok ? "נשמר בהצלחה" : "שגיאה בשמירה";
}

async function loadSubmissions() {
    const key = getKey();
    if (!key) return alert("יש לשמור קודם מפתח מרצה");

    const status = document.getElementById("loadStatus");
    status.textContent = "טוען...";

    const response = await fetch("/teacher/submissions", {
        headers: {"X-Teacher-Key": key}
    });

    if (!response.ok) {
        status.textContent = "שגיאה";
        return;
    }

    const data = await response.json();
    const tbody = document.getElementById("submissionRows");
    tbody.innerHTML = "";

    for (const item of data.submissions) {
        const tr = document.createElement("tr");

        const values = [
            item.student_code || "",
            item.course_id || "",
            item.group_id || "",
            item.quiz_id || "",
            item.score ?? "",
            item.status || "",
            item.created_at || ""
        ];

        for (const value of values) {
            const td = document.createElement("td");
            td.textContent = value;
            tr.appendChild(td);
        }

        tbody.appendChild(tr);
    }

    status.textContent = data.submissions.length + " הגשות";
}
</script>
</body>
</html>
"""


# ==========================================================
# HEALTH
# ==========================================================

@app.get("/health")
def health():
    return {"ok": True}


# ==========================================================
# TEACHER PAGE
# ==========================================================

@app.get("/teacher")
def teacher_page():
    return render_template_string(TEACHER_PAGE)


# ==========================================================
# STUDENT SUBMISSION
# ==========================================================

@app.get("/student")
def student_page():
    return """
    <!doctype html>
    <html lang="he" dir="rtl">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Math Control Test</title>
    </head>
    <body>
        <h1>Math Control Test</h1>
        <p>Use the GitHub Pages student form.</p>
    </body>
    </html>
    """


@app.post("/submit")
def submit():
    code = request.form.get("student_code", "").strip()
    pin = request.form.get("pin", "")
    quiz_id = request.form.get("quiz_id", "").strip()

    course_id = clean_part(
        request.form.get("course_id", ""),
        "general"
    )
    group_id = clean_part(
        request.form.get("group_id", ""),
        "general"
    )

    files = request.files.getlist("files")
    files = [f for f in files if f and f.filename]

    if not code or not pin or not quiz_id or not files:
        return jsonify(error="חסרים שדות חובה"), 400

    allowed = {".png", ".jpg", ".jpeg"}

    for f in files:
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in allowed:
            return jsonify(error="ניתן להעלות רק קבצי PNG או JPG"), 400

    quiz_key = make_quiz_key(course_id, group_id, quiz_id)

    sid = str(uuid.uuid4())
    uploaded_paths = []

    try:
        for index, f in enumerate(files, start=1):
            ext = os.path.splitext(f.filename)[1].lower()

            # Keep storage path simple and independent of Hebrew/course names.
            path = f"{code}/{sid}/{index:03d}{ext}"
            content = f.read()

            sb.storage.from_("mini-check-files").upload(
                path,
                content,
                {
                    "content-type": f.mimetype or "application/octet-stream",
                    "upsert": "false"
                }
            )

            uploaded_paths.append(path)

        sb.table("mini_check_submissions").insert({
            "id": sid,
            "student_code": code,
            "pin_hash": phash(code, pin),
            "quiz_id": quiz_key,
            "file_path": json.dumps(uploaded_paths, ensure_ascii=False),
            "status": "submitted"
        }).execute()

        # The student decides when to start AI grading.
        return jsonify(
            submission_id=sid,
            files=len(uploaded_paths),
            course_id=course_id,
            group_id=group_id,
            quiz_id=quiz_id,
            status="submitted",
            score=None,
            feedback="העבודה התקבלה. ניתן להתחיל בדיקת AI."
        )

    except Exception as e:
        print("SUBMIT ERROR:", e)

        for path in uploaded_paths:
            try:
                sb.storage.from_("mini-check-files").remove([path])
            except Exception:
                pass

        message = str(e).lower()

        if "duplicate" in message or "unique" in message or "23505" in message:
            return jsonify(error="העבודה הזאת כבר הוגשה"), 409

        return jsonify(error="שגיאה בשמירת ההגשה"), 500

# ==========================================================
# STUDENT – FIND EXISTING SUBMISSION
# ==========================================================

@app.post("/student/submission")
def student_existing_submission():
    d = request.get_json(force=True)

    code = str(d.get("student_code", "")).strip()
    pin = str(d.get("pin", ""))
    course_id = clean_part(d.get("course_id", ""), "general")
    group_id = clean_part(d.get("group_id", ""), "general")
    quiz_id = clean_part(d.get("quiz_id", ""), "1")

    if not code or not pin:
        return jsonify(error="חסרים קוד סטודנט או PIN"), 400

    quiz_key = make_quiz_key(course_id, group_id, quiz_id)

    try:
        rows = (
            sb.table("mini_check_submissions")
            .select(
                "id,"
                "student_code,"
                "quiz_id,"
                "status,"
                "score,"
                "feedback,"
                "created_at"
            )
            .eq("student_code", code)
            .eq("pin_hash", phash(code, pin))
            .eq("quiz_id", quiz_key)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
            .data
        )

        if not rows:
            return jsonify(found=False)

        row = rows[0]

        return jsonify(
            found=True,
            submission_id=row.get("id"),
            status=row.get("status"),
            score=row.get("score"),
            feedback=row.get("feedback") or "",
            course_id=course_id,
            group_id=group_id,
            quiz_id=quiz_id
        )

    except Exception as e:
        print("STUDENT FIND SUBMISSION ERROR:", e)
        return jsonify(error="שגיאה בחיפוש ההגשה"), 500

# ==========================================================
# STUDENT – START AI GRADING
# ==========================================================

@app.post("/student/grade")
def student_grade():
    d = request.get_json(force=True)

    submission_id = str(d.get("submission_id", "")).strip()
    code = str(d.get("student_code", "")).strip()
    pin = str(d.get("pin", ""))

    if not submission_id or not code or not pin:
        return jsonify(error="חסרים פרטים לבדיקה"), 400

    rows = (
        sb.table("mini_check_submissions")
        .select(
            "id,"
            "student_code,"
            "pin_hash,"
            "quiz_id,"
            "file_path,"
            "status,"
            "score,"
            "feedback"
        )
        .eq("id", submission_id)
        .limit(1)
        .execute()
        .data
    )

    if not rows:
        return jsonify(error="ההגשה לא נמצאה"), 404

    row = rows[0]

    if row.get("student_code") != code or row.get("pin_hash") != phash(code, pin):
        return jsonify(error="קוד סטודנט או PIN שגויים"), 403

    if row.get("status") == "graded" and row.get("score") is not None:
        return jsonify(
            submission_id=submission_id,
            status="graded",
            score=row.get("score"),
            feedback=row.get("feedback") or ""
        )

    raw_file_path = row.get("file_path") or ""

    if isinstance(raw_file_path, list):
        uploaded_paths = raw_file_path
    else:
        try:
            parsed = json.loads(str(raw_file_path))
            uploaded_paths = parsed if isinstance(parsed, list) else [parsed]
        except Exception:
            uploaded_paths = [str(raw_file_path)]

    uploaded_paths = [
        str(path).strip()
        for path in uploaded_paths
        if str(path).strip()
    ]

    if not uploaded_paths:
        return jsonify(error="לא נמצאו קבצים לבדיקה"), 400

    try:
        sb.table("mini_check_submissions").update({
            "status": "grading",
            "score": None
        }).eq("id", submission_id).execute()

        grade_result = auto_grade_submission(
            submission_id=submission_id,
            quiz_key=row.get("quiz_id", ""),
            uploaded_paths=uploaded_paths
        )

        return jsonify(
            submission_id=submission_id,
            status=grade_result["status"],
            score=grade_result["score"],
            feedback=grade_result["feedback"]
        )

    except Exception as e:
        print("STUDENT GRADE ERROR:", e)
        return jsonify(error="שגיאה בבדיקת AI"), 500
# ==========================================================
# STUDENT – DELETE SUBMISSION BEFORE GRADING
# ==========================================================

@app.post("/student/delete")
def student_delete_submission():
    d = request.get_json(force=True)

    submission_id = str(d.get("submission_id", "")).strip()
    code = str(d.get("student_code", "")).strip()
    pin = str(d.get("pin", ""))

    if not submission_id or not code or not pin:
        return jsonify(error="חסרים פרטים למחיקת ההגשה"), 400

    rows = (
        sb.table("mini_check_submissions")
        .select(
            "id,"
            "student_code,"
            "pin_hash,"
            "file_path,"
            "status"
        )
        .eq("id", submission_id)
        .limit(1)
        .execute()
        .data
    )

    if not rows:
        return jsonify(error="ההגשה לא נמצאה"), 404

    row = rows[0]

    if (
        row.get("student_code") != code
        or row.get("pin_hash") != phash(code, pin)
    ):
        return jsonify(error="קוד סטודנט או PIN שגויים"), 403

    if row.get("status") != "submitted":
        return jsonify(
            error="לא ניתן למחוק עבודה לאחר תחילת הבדיקה"
        ), 409

    raw_file_path = row.get("file_path") or ""

    if isinstance(raw_file_path, list):
        paths = raw_file_path
    else:
        try:
            parsed = json.loads(str(raw_file_path))
            paths = parsed if isinstance(parsed, list) else [parsed]
        except Exception:
            paths = [str(raw_file_path)]

    paths = [
        str(path).strip()
        for path in paths
        if str(path).strip()
    ]

    try:
        if paths:
            sb.storage.from_("mini-check-files").remove(paths)

        sb.table("mini_check_submissions") \
            .delete() \
            .eq("id", submission_id) \
            .execute()

        return jsonify(
            deleted=True,
            submission_id=submission_id
        )

    except Exception as e:
        print("STUDENT DELETE ERROR:", e)
        return jsonify(error="שגיאה במחיקת ההגשה"), 500

# ==========================================================
# STUDENT RESULTS
# ==========================================================

@app.post("/results")
def results():
    d = request.get_json(force=True)

    code = d.get("student_code", "").strip()
    p = phash(code, d.get("pin", ""))

    rows = (
        sb.table("mini_check_submissions")
        .select(
            "quiz_id,"
            "score,"
            "feedback,"
            "status,"
            "created_at"
        )
        .eq("student_code", code)
        .eq("pin_hash", p)
        .order("created_at", desc=True)
        .execute()
        .data
    )

    public_rows = []

    for row in rows:
        parts = split_quiz_key(row.get("quiz_id"))

        public_rows.append({
            "course_id": parts["course_id"],
            "group_id": parts["group_id"],
            "quiz_id": parts["quiz_id"],
            "score": row.get("score"),
            "feedback": row.get("feedback"),
            "status": row.get("status"),
            "created_at": row.get("created_at"),
        })

    return jsonify(results=public_rows)


# ==========================================================
# TEACHER – SAVE PROBLEM / RUBRIC
# ==========================================================

@app.post("/teacher/rubric")
def rubric():
    if not teacher_ok():
        return jsonify(error="Forbidden"), 403

    d = request.get_json(force=True)

    course_id = clean_part(d.get("course_id", ""), "general")
    group_id = clean_part(d.get("group_id", ""), "general")
    quiz_id = clean_part(d.get("quiz_id", ""), "1")

    quiz_key = make_quiz_key(course_id, group_id, quiz_id)

    sb.table("mini_check_rubrics").upsert({
        "quiz_id": quiz_key,
        "problem": d.get("problem", ""),
        "rubric": d.get("rubric", "")
    }).execute()

    return {
        "ok": True,
        "course_id": course_id,
        "group_id": group_id,
        "quiz_id": quiz_id,
    }


# ==========================================================
# TEACHER – LIST SUBMISSIONS
# ==========================================================

@app.get("/teacher/submissions")
def submissions():
    if not teacher_ok():
        return jsonify(error="Forbidden"), 403

    rows = (
        sb.table("mini_check_submissions")
        .select(
            "id,"
            "student_code,"
            "quiz_id,"
            "score,"
            "feedback,"
            "status,"
            "created_at,"
            "file_path"
        )
        .order("created_at", desc=True)
        .execute()
        .data
    )

    public_rows = []

    for row in rows:
        parts = split_quiz_key(row.get("quiz_id"))

        item = dict(row)
        item["course_id"] = parts["course_id"]
        item["group_id"] = parts["group_id"]
        item["quiz_id"] = parts["quiz_id"]

        public_rows.append(item)

    return jsonify(submissions=public_rows)


@app.post("/teacher/file-url")
def teacher_file_url():
    d = request.get_json(force=True)

    teacher_password = d.get("teacher_password", "").strip()
    raw_file_path = d.get("file_path", "")

    if not teacher_password:
        return jsonify(error="Missing teacher password"), 400

    if raw_file_path is None or str(raw_file_path).strip() == "":
        return jsonify(error="Missing file_path"), 400

    try:
        sb.rpc(
            "get_teacher_submissions",
            {"p_teacher_password": teacher_password}
        ).execute()
    except Exception:
        return jsonify(error="Invalid teacher password"), 403

    if isinstance(raw_file_path, list):
        paths = raw_file_path
    else:
        text = str(raw_file_path).strip()
        try:
            parsed = json.loads(text)
            paths = parsed if isinstance(parsed, list) else [parsed]
        except Exception:
            paths = [text]

    paths = [str(p).strip() for p in paths if str(p).strip()]

    if not paths:
        return jsonify(error="No files found"), 400

    signed_urls = []

    for path in paths:
        result = (
            sb.storage
            .from_("mini-check-files")
            .create_signed_url(path, 900)
        )

        signed_url = (
            result.get("signedURL")
            or result.get("signedUrl")
            or result.get("signed_url")
        )

        if not signed_url:
            return jsonify(error="Could not create signed URL"), 500

        signed_urls.append(signed_url)

    return jsonify(signedUrls=signed_urls)


@app.post("/teacher/exam-upload")
def teacher_exam_upload():
    teacher_password = request.form.get("teacher_password", "").strip()
    exam_code = request.form.get("exam_code", "").strip()
    file = request.files.get("file")

    if not teacher_password:
        return jsonify(error="Missing teacher password"), 400

    if not exam_code:
        return jsonify(error="Missing exam code"), 400

    if not file:
        return jsonify(error="Missing exam file"), 400

    try:
        sb.rpc(
            "get_teacher_submissions",
            {"p_teacher_password": teacher_password}
        ).execute()
    except Exception:
        return jsonify(error="Invalid teacher password"), 403

    try:
        file_bytes = file.read()
        extension = os.path.splitext(file.filename)[1].lower()

        if extension not in [".pdf", ".png", ".jpg", ".jpeg"]:
            return jsonify(error="Unsupported file type"), 400

        file_path = (
            "exams/"
            + exam_code
            + "/questions"
            + extension
        )

        # Replace an older copy safely. Some storage client versions do not
        # honor string-valued upsert consistently.
        try:
            sb.storage.from_("submissions").remove([file_path])
        except Exception:
            pass

        sb.storage.from_("submissions").upload(
            file_path,
            file_bytes,
            {
                "content-type": file.content_type or "application/octet-stream",
                "upsert": "false"
            }
        )

        # MATH-3 -> v2::MATH::general::3.  Register a default rubric so the
        # uploaded official exam immediately enables automatic grading.
        if "-" in exam_code:
            course_id, quiz_id = exam_code.rsplit("-", 1)
            if course_id and quiz_id:
                quiz_key = make_quiz_key(course_id, "general", quiz_id)
                sb.table("mini_check_rubrics").upsert({
                    "quiz_id": quiz_key,
                    "problem": "Use the attached official exam/questions file.",
                    "rubric": (
                        "Grade the entire submitted exam out of 100. "
                        "Use the official exam/questions file as the source of the questions. "
                        "Give 0 points for questions or subquestions that were not answered. "
                        "Deduct points proportionally for mathematical errors and incomplete reasoning. "
                        "Accept any mathematically valid solution method. "
                        "Do not rescale a partially submitted exam to 100."
                    )
                }, on_conflict="quiz_id").execute()

        return jsonify(
            success=True,
            exam_code=exam_code,
            file_path=file_path
        )

    except Exception as e:
        print("EXAM UPLOAD ERROR:", e)
        return jsonify(error=str(e)), 500


# ==========================================================
# START
# ==========================================================

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "10000"))
    )
