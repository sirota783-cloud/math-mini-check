import os
import hashlib
import uuid

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


def phash(code, pin):
    return hashlib.sha256(
        (code + "|" + pin).encode()
    ).hexdigest()


def teacher_ok():
    return (
        request.headers.get("X-Teacher-Key", "")
        == TEACHER_KEY
    )


# ==========================================================
# TEACHER PAGE
# ==========================================================

TEACHER_PAGE = """
<!doctype html>
<html lang="en">

<head>
    <meta charset="utf-8">
    <meta name="viewport"
          content="width=device-width, initial-scale=1">

    <title>Teacher – Math Control Tests</title>

    <style>
        body {
            font-family: Arial, sans-serif;
            max-width: 1000px;
            margin: 35px auto;
            padding: 0 20px;
        }

        input, textarea, button {
            font-size: 16px;
            margin: 6px 0;
        }

        input, textarea {
            width: 100%;
            box-sizing: border-box;
            padding: 8px;
        }

        textarea {
            min-height: 100px;
        }

        button {
            padding: 8px 16px;
            cursor: pointer;
        }

        .box {
            border: 1px solid #ccc;
            border-radius: 8px;
            padding: 18px;
            margin-bottom: 25px;
        }

        table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 15px;
        }

        th, td {
            border: 1px solid #ddd;
            padding: 8px;
            text-align: left;
        }

        th {
            background: #f4f4f4;
        }

        .status {
            margin-top: 10px;
            font-weight: bold;
        }

        .small {
            font-size: 13px;
            color: #555;
        }
    </style>
</head>


<body>

<h1>Teacher – Math Control Tests</h1>


<div class="box">

    <h2>Teacher access</h2>

    <label>Teacher key</label>

    <input
        id="teacherKey"
        type="password"
        placeholder="Enter teacher key"
    >

    <button onclick="saveKey()">
        Save key on this computer
    </button>

    <div class="small">
        The key is stored only in this browser.
    </div>

</div>


<div class="box">

    <h2>Create / update problem</h2>

    <label>Quiz ID</label>

    <input
        id="quizId"
        placeholder="Example: control-01-problem-1"
    >


    <label>Problem</label>

    <textarea
        id="problem"
        placeholder="Paste the exact problem here">
    </textarea>


    <label>Grading instructions / rubric</label>

    <textarea
        id="rubric"
        placeholder="Example: 10 points total. Deduct 2 points for ...">
    </textarea>


    <button onclick="saveProblem()">
        Save problem
    </button>

    <div
        id="saveStatus"
        class="status">
    </div>

</div>


<div class="box">

    <h2>Student submissions</h2>

    <button onclick="loadSubmissions()">
        Refresh submissions
    </button>

    <div
        id="loadStatus"
        class="status">
    </div>


    <table>

        <thead>
        <tr>
            <th>Student</th>
            <th>Quiz</th>
            <th>Score</th>
            <th>Status</th>
            <th>Submitted</th>
            <th>File</th>
        </tr>
        </thead>

        <tbody id="submissionRows">
        </tbody>

    </table>

</div>


<script>

function getKey() {
    return localStorage.getItem("teacher_key") || "";
}


function saveKey() {

    const key =
        document.getElementById("teacherKey").value.trim();

    if (!key) {
        alert("Enter teacher key");
        return;
    }

    localStorage.setItem(
        "teacher_key",
        key
    );

    alert("Teacher key saved");
}


window.addEventListener(
    "load",
    () => {

        const key = getKey();

        if (key) {
            document.getElementById(
                "teacherKey"
            ).value = key;
        }
    }
);


async function saveProblem() {

    const key = getKey();

    if (!key) {
        alert("Enter and save teacher key first");
        return;
    }


    const quizId =
        document.getElementById(
            "quizId"
        ).value.trim();

    const problem =
        document.getElementById(
            "problem"
        ).value.trim();

    const rubric =
        document.getElementById(
            "rubric"
        ).value.trim();


    if (!quizId) {
        alert("Quiz ID is required");
        return;
    }


    const status =
        document.getElementById(
            "saveStatus"
        );

    status.textContent =
        "Saving...";


    try {

        const response =
            await fetch(
                "/teacher/rubric",
                {
                    method: "POST",

                    headers: {
                        "Content-Type":
                            "application/json",

                        "X-Teacher-Key":
                            key
                    },

                    body: JSON.stringify({
                        quiz_id: quizId,
                        problem: problem,
                        rubric: rubric
                    })
                }
            );


        if (!response.ok) {

            const text =
                await response.text();

            throw new Error(text);
        }


        status.textContent =
            "Saved successfully.";

    }

    catch (error) {

        console.error(error);

        status.textContent =
            "Error saving problem.";
    }
}



async function loadSubmissions() {

    const key = getKey();

    if (!key) {
        alert("Enter and save teacher key first");
        return;
    }


    const status =
        document.getElementById(
            "loadStatus"
        );

    status.textContent =
        "Loading...";


    try {

        const response =
            await fetch(
                "/teacher/submissions",
                {
                    headers: {
                        "X-Teacher-Key":
                            key
                    }
                }
            );


        if (!response.ok) {

            const text =
                await response.text();

            throw new Error(text);
        }


        const data =
            await response.json();


        const tbody =
            document.getElementById(
                "submissionRows"
            );


        tbody.innerHTML = "";


        for (const item of data.submissions) {

            const tr =
                document.createElement("tr");


            const values = [

                item.student_code || "",

                item.quiz_id || "",

                item.score === null
                    ? ""
                    : item.score,

                item.status || "",

                item.created_at || "",

                item.file_path || ""
            ];


            for (const value of values) {

                const td =
                    document.createElement("td");

                td.textContent =
                    value;

                tr.appendChild(td);
            }


            tbody.appendChild(tr);
        }


        status.textContent =
            `${data.submissions.length} submissions`;

    }

    catch (error) {

        console.error(error);

        status.textContent =
            "Error loading submissions.";
    }
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
    return render_template_string(
        TEACHER_PAGE
    )


# ==========================================================
# STUDENT SUBMISSION
# ==========================================================
@app.get("/student")
def student_page():
    return """
    <!doctype html>
    <html lang="en">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Math Control Test</title>
        <style>
            body {
                font-family: Arial, sans-serif;
                max-width: 700px;
                margin: 40px auto;
                padding: 0 20px;
            }
            input, button {
                font-size: 16px;
                margin: 8px 0;
                padding: 8px;
            }
            input {
                width: 100%;
                box-sizing: border-box;
            }
        </style>
    </head>
    <body>

        <h1>Math Control Test</h1>

        <form method="post"
              action="/submit"
              enctype="multipart/form-data">

            <label>Student code</label>
            <input type="text"
                   name="student_code"
                   required>

            <label>PIN</label>
            <input type="password"
                   name="pin"
                   required>

            <label>Quiz ID</label>
            <input type="text"
                   name="quiz_id"
                   required>

            <label>Upload handwritten solution</label>
            <input type="file"
                   name="file"
                   accept="image/*,application/pdf"
                   required>

            <br>

            <button type="submit">
                Submit solution
            </button>

        </form>

    </body>
    </html>
    """
@app.post("/submit")
def submit():

    code = request.form.get(
        "student_code",
        ""
    ).strip()

    pin = request.form.get(
        "pin",
        ""
    )

    quiz_id = request.form.get(
        "quiz_id",
        ""
    ).strip()

    f = request.files.get(
        "file"
    )


    if (
        not code
        or not pin
        or not quiz_id
        or not f
    ):
        return jsonify(
            error="חסרים שדות חובה"
        ), 400


    sid = str(
        uuid.uuid4()
    )


    ext = (
        os.path.splitext(
            f.filename
        )[1].lower()
        or ".bin"
    )


    path = (
        f"{quiz_id}/"
        f"{sid}{ext}"
    )


    content = f.read()


    sb.storage.from_(
        "submissions"
    ).upload(
        path,
        content,
        {
            "content-type":
                f.mimetype,

            "upsert":
                "false"
        }
    )


    sb.table(
      "mini_check_submissions"
    ).insert({
        "id":
            sid,

        "student_code":
            code,

        "pin_hash":
            phash(
                code,
                pin
            ),

        "quiz_id":
            quiz_id,

        "file_path":
            path,

        "status":
            "submitted"
    }).execute()


    return jsonify(
        submission_id=sid
    )


# ==========================================================
# STUDENT RESULTS
# ==========================================================

@app.post("/results")
def results():

    d = request.get_json(
        force=True
    )


    code = d.get(
        "student_code",
        ""
    ).strip()


    p = phash(
        code,
        d.get(
            "pin",
            ""
        )
    )


    rows = (
        sb.table("mini_check_submissions")
        .select(
            "quiz_id,"
            "score,"
            "feedback,"
            "status,"
            "created_at"
        )
        .eq(
            "student_code",
            code
        )
        .eq(
            "pin_hash",
            p
        )
        .order(
            "created_at",
            desc=True
        )
        .execute()
        .data
    )


    return jsonify(
        results=rows
    )


# ==========================================================
# TEACHER – SAVE PROBLEM / RUBRIC
# ==========================================================

@app.post("/teacher/rubric")
def rubric():

    if not teacher_ok():

        return jsonify(
            error="Forbidden"
        ), 403


    d = request.get_json(
        force=True
    )


    sb.table(
        "rubrics"
    ).upsert({

        "quiz_id":
            d["quiz_id"],

        "problem":
            d.get(
                "problem",
                ""
            ),

        "rubric":
            d.get(
                "rubric",
                ""
            )

    }).execute()


    return {
        "ok": True
    }


# ==========================================================
# TEACHER – LIST SUBMISSIONS
# ==========================================================

@app.get("/teacher/submissions")
def submissions():

    if not teacher_ok():

        return jsonify(
            error="Forbidden"
        ), 403


    rows = (
        sb.table("mini_check_submissions")
        .select(
            "id,"
            "student_code,"
            "quiz_id,"
            "score,"
            "status,"
            "created_at,"
            "file_path"
        )
        .order(
            "created_at",
            desc=True
        )
        .execute()
        .data
    )


    return jsonify(
        submissions=rows
    )
@app.post("/teacher/file-url")
def teacher_file_url():
    d = request.get_json(force=True)

    teacher_password = d.get("teacher_password", "").strip()
    file_path = d.get("file_path", "").strip()

    if not teacher_password:
        return jsonify(error="Missing teacher password"), 400

    if not file_path:
        return jsonify(error="Missing file_path"), 400

    try:
        sb.rpc(
            "get_teacher_submissions",
            {"p_teacher_password": teacher_password}
        ).execute()
    except Exception:
        return jsonify(error="Invalid teacher password"), 403

    result = (
        sb.storage
        .from_("submissions")
        .create_signed_url(file_path, 300)
    )

    signed_url = (
        result.get("signedURL")
        or result.get("signedUrl")
        or result.get("signed_url")
    )

    if not signed_url:
        return jsonify(error="Could not create signed URL"), 500

    return jsonify(signedUrl=signed_url)
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

    # Проверяем пароль преподавателя
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

        sb.storage.from_("submissions").upload(
            file_path,
            file_bytes,
            {
                "content-type": file.content_type,
                "upsert": "true"
            }
        )

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
        port=int(
            os.environ.get(
                "PORT",
                "10000"
            )
        )
    )
