import os
import hashlib
import uuid
import json
import base64
import re
import io
import zipfile
import urllib.request
import urllib.error
from datetime import datetime, timezone
from xml.sax.saxutils import escape

from flask import Flask, request, jsonify, render_template_string, send_file
from flask_cors import CORS
from supabase import create_client


app = Flask(__name__)
CORS(app)

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

sb = create_client(
    SUPABASE_URL,
    SUPABASE_SERVICE_KEY
)

TEACHER_KEY = os.environ["TEACHER_KEY"]
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5").strip()
AUTO_GRADE_CONFIDENCE = float(os.environ.get("AUTO_GRADE_CONFIDENCE", "0.75"))


def phash(code, pin):
    return hashlib.sha256(
        (code + "|" + pin).encode()
    ).hexdigest()


def normalize_student_code(value):
    """
    Make harmless spelling differences equivalent:
    "Math 1004", "math-1004" and "MATH-1004" -> "MATH-1004".
    """
    value = str(value or "").strip().upper()
    value = re.sub(r"[\s_]+", "-", value)
    value = re.sub(r"-+", "-", value)
    return value.strip("-")


def teacher_ok():
    return (
        request.headers.get("X-Teacher-Key", "")
        == TEACHER_KEY
    )


def student_access_identity(code, pin):
    """
    Resolve an active code/PIN without breaking historical mixed-case codes.

    PIN hashes in existing rows were calculated from the spelling originally
    used when the student was registered. Therefore each candidate must be
    checked with its stored code, while new submissions use a canonical code.
    """
    canonical_code = normalize_student_code(code)
    if not canonical_code or not pin:
        return None

    rows = (
        sb.table("mini_check_student_access")
        .select("student_code,pin_hash")
        .eq("active", True)
        .execute()
        .data
    )

    for row in rows:
        stored_code = str(row.get("student_code", "")).strip()
        stored_hash = str(row.get("pin_hash", ""))

        if (
            normalize_student_code(stored_code) == canonical_code
            and stored_hash == phash(stored_code, pin)
        ):
            return {
                "student_code": canonical_code,
                "pin_hash": stored_hash,
                "registered_code": stored_code,
            }

    return None


def student_access_ok(code, pin):
    """Compatibility wrapper for simple access checks."""
    return student_access_identity(code, pin) is not None


def teacher_password_ok(password):
    if not password:
        return False
    try:
        sb.rpc(
            "get_teacher_submissions",
            {"p_teacher_password": password}
        ).execute()
        return True
    except Exception:
        return False


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


def normalized_submission_status(row):
    value = str(row.get("status", "")).strip().lower()
    if value == "graded" and row.get("score") is not None:
        return "graded"
    if value in {"submitted", "grading", "needs_teacher", "error"}:
        return value
    return "graded" if row.get("score") is not None else "submitted"


def grouped_submission_rows(rows):
    """Match the grouping shown on the teacher page."""
    groups = {}

    for row in rows:
        parts = split_quiz_key(row.get("quiz_id"))
        course_id = parts.get("course_id") or "legacy"
        group_id = parts.get("group_id") or "general"
        quiz_id = parts.get("quiz_id") or ""
        student_code = normalize_student_code(row.get("student_code"))
        key = (course_id, group_id, quiz_id, student_code)

        group = groups.setdefault(key, {
            "student_code": student_code,
            "course_id": course_id,
            "group_id": group_id,
            "quiz_id": quiz_id,
            "rows": [],
        })
        group["rows"].append(row)

    output = []

    for group in groups.values():
        statuses = [
            normalized_submission_status(row)
            for row in group["rows"]
        ]

        if "error" in statuses:
            status = "error"
        elif "grading" in statuses:
            status = "grading"
        elif "needs_teacher" in statuses:
            status = "needs_teacher"
        elif statuses and all(value == "graded" for value in statuses):
            status = "graded"
        else:
            status = "submitted"

        numeric_scores = []
        for row in group["rows"]:
            value = row.get("score")
            if value is None:
                continue
            try:
                numeric_scores.append(float(value))
            except (TypeError, ValueError):
                continue

        unique_scores = sorted(set(numeric_scores))
        score = unique_scores[0] if len(unique_scores) == 1 else None
        if isinstance(score, float) and score.is_integer():
            score = int(score)

        feedback_values = []
        for row in group["rows"]:
            value = str(row.get("feedback", "") or "").strip()
            if value and value not in feedback_values:
                feedback_values.append(value)

        if len(feedback_values) == 1:
            feedback = feedback_values[0]
        elif len(feedback_values) > 1:
            feedback = "קיימות הערות שונות בין חלקי ההגשה"
        else:
            feedback = ""

        dates = [
            str(row.get("created_at", "") or "").strip()
            for row in group["rows"]
            if row.get("created_at")
        ]

        output.append({
            **{k: v for k, v in group.items() if k != "rows"},
            "status": status,
            "score": score,
            "feedback": feedback,
            "created_at": max(dates) if dates else "",
            "has_no_score": len(numeric_scores) == 0,
        })

    return output


def excel_column_name(number):
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def excel_datetime_serial(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        origin = datetime(1899, 12, 30)
        return (parsed - origin).total_seconds() / 86400
    except (TypeError, ValueError):
        return None


def build_grades_xlsx(records):
    """Build a compact, right-to-left Excel workbook without extra packages."""
    headers = [
        "קוד סטודנט",
        "מקצוע",
        "קבוצה",
        "מספר תרגול",
        "ציון",
        "מצב",
        "תאריך הגשה",
        "הערה",
    ]

    status_labels = {
        "submitted": "לא נבדק",
        "grading": "בבדיקה",
        "graded": "נבדק",
        "needs_teacher": "לבדיקת מרצה",
        "error": "שגיאה",
    }
    course_labels = {
        "MATH": "מתמטיקה — קורס הכנה",
        "DISCRETE": "מתמטיקה בדידה",
        "legacy": "הגשות ישנות",
        "general": "כללי",
    }

    table_rows = []
    for record in records:
        table_rows.append([
            record.get("student_code", ""),
            course_labels.get(
                record.get("course_id"),
                record.get("course_id", "")
            ),
            record.get("group_id", ""),
            record.get("quiz_id", ""),
            record.get("score"),
            status_labels.get(
                record.get("status"),
                record.get("status", "")
            ),
            record.get("created_at", ""),
            record.get("feedback", ""),
        ])

    def inline_cell(reference, value, style=2):
        text = escape(str(value if value is not None else ""))
        return (
            f'<c r="{reference}" t="inlineStr" s="{style}">'
            f'<is><t xml:space="preserve">{text}</t></is></c>'
        )

    def number_cell(reference, value, style=4):
        return f'<c r="{reference}" t="n" s="{style}"><v>{value}</v></c>'

    xml_rows = []
    header_cells = [
        inline_cell(f"{excel_column_name(index)}1", value, 1)
        for index, value in enumerate(headers, start=1)
    ]
    xml_rows.append(
        '<row r="1" ht="24" customHeight="1">'
        + "".join(header_cells)
        + "</row>"
    )

    for row_number, values in enumerate(table_rows, start=2):
        cells = []
        for column_number, value in enumerate(values, start=1):
            reference = f"{excel_column_name(column_number)}{row_number}"
            if column_number == 5 and value is not None:
                cells.append(number_cell(reference, value, 4))
            elif column_number == 7:
                serial = excel_datetime_serial(value)
                if serial is None:
                    cells.append(inline_cell(reference, value, 2))
                else:
                    cells.append(number_cell(reference, serial, 3))
            else:
                cells.append(inline_cell(reference, value, 2))
        xml_rows.append(f'<row r="{row_number}">' + "".join(cells) + "</row>")

    last_row = max(1, len(table_rows) + 1)
    created = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    sheet_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <dimension ref="A1:H{last_row}"/>
  <sheetViews><sheetView workbookViewId="0" rightToLeft="1"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>
  <sheetFormatPr defaultRowHeight="18"/>
  <cols>
    <col min="1" max="1" width="18" customWidth="1"/>
    <col min="2" max="2" width="28" customWidth="1"/>
    <col min="3" max="3" width="14" customWidth="1"/>
    <col min="4" max="4" width="14" customWidth="1"/>
    <col min="5" max="5" width="11" customWidth="1"/>
    <col min="6" max="6" width="18" customWidth="1"/>
    <col min="7" max="7" width="21" customWidth="1"/>
    <col min="8" max="8" width="55" customWidth="1"/>
  </cols>
  <sheetData>{''.join(xml_rows)}</sheetData>
  <autoFilter ref="A1:H{last_row}"/>
</worksheet>'''

    styles_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy-mm-dd hh:mm"/></numFmts>
  <fonts count="2">
    <font><sz val="11"/><name val="Arial"/></font>
    <font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Arial"/></font>
  </fonts>
  <fills count="3">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF4472C4"/><bgColor indexed="64"/></patternFill></fill>
  </fills>
  <borders count="2">
    <border><left/><right/><top/><bottom/><diagonal/></border>
    <border><left style="thin"><color rgb="FFD9E2F3"/></left><right style="thin"><color rgb="FFD9E2F3"/></right><top style="thin"><color rgb="FFD9E2F3"/></top><bottom style="thin"><color rgb="FFD9E2F3"/></bottom><diagonal/></border>
  </borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="5">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyAlignment="1"><alignment horizontal="right" vertical="center" wrapText="1"/></xf>
    <xf numFmtId="164" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
  </cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>'''

    files = {
        "[Content_Types].xml": '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>''',
        "_rels/.rels": '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>''',
        "docProps/app.xml": '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"><Application>Math Mini Check</Application></Properties>''',
        "docProps/core.xml": f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><dc:creator>Leonid Sirota</dc:creator><dc:title>Math Mini Check Grades</dc:title><dcterms:created xsi:type="dcterms:W3CDTF">{created}</dcterms:created></cp:coreProperties>''',
        "xl/workbook.xml": '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><bookViews><workbookView/></bookViews><sheets><sheet name="ציונים" sheetId="1" r:id="rId1"/></sheets></workbook>''',
        "xl/_rels/workbook.xml.rels": '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>''',
        "xl/styles.xml": styles_xml,
        "xl/worksheets/sheet1.xml": sheet_xml,
    }

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, content in files.items():
            archive.writestr(path, content.encode("utf-8"))
    output.seek(0)
    return output


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


def signed_image_urls(uploaded_paths, expires_in=900):
    """Create short-lived URLs for the existing Supabase AI function."""
    urls = []

    for path in uploaded_paths:
        result = (
            sb.storage
            .from_("mini-check-files")
            .create_signed_url(path, expires_in)
        )

        signed_url = (
            result.get("signedURL")
            or result.get("signedUrl")
            or result.get("signed_url")
        )

        if not signed_url:
            raise RuntimeError(
                f"Could not create signed URL for {path}"
            )

        urls.append(signed_url)

    return urls


def edge_function_grade(quiz_key, image_urls):
    """
    Use the same Supabase AI checker that already works on the teacher page.
    This avoids requiring a second OpenAI key/configuration on Render.
    """
    body = json.dumps({
        "image_urls": image_urls,
        "quiz_id": quiz_key,
    }).encode("utf-8")

    req = urllib.request.Request(
        SUPABASE_URL + "/functions/v1/ai-check-submission",
        data=body,
        method="POST",
        headers={
            "apikey": SUPABASE_SERVICE_KEY,
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        details = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Supabase AI HTTP {e.code}: {details[:500]}"
        )
    except Exception as e:
        raise RuntimeError(f"Supabase AI request failed: {e}")

    score = payload.get("score")
    if score is not None:
        score = float(score)
        if score.is_integer():
            score = int(score)

    confidence_value = payload.get("confidence")
    confidence = (
        float(confidence_value)
        if confidence_value is not None
        else 1.0
    )

    status = str(payload.get("status", "")).strip().lower()
    needs_teacher = bool(payload.get("needs_teacher", False))
    if status in {"needs_teacher", "teacher_review"}:
        needs_teacher = True

    return {
        "score": score,
        "feedback": str(payload.get("feedback", "")).strip(),
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
        grade = None

        # Primary path: reuse the Supabase Edge Function already used by the
        # teacher page. It has the working OpenAI configuration and exam-file
        # lookup, so student auto-grading behaves exactly like manual grading.
        try:
            image_urls = signed_image_urls(uploaded_paths)
            grade = edge_function_grade(quiz_key, image_urls)
        except Exception as edge_error:
            print("SUPABASE EDGE GRADE ERROR:", edge_error, flush=True)

        # Optional fallback for installations that also configured an OpenAI
        # key on Render. The current deployment can work without this key.
        if grade is None:
            if not OPENAI_API_KEY:
                raise RuntimeError(
                    "Supabase AI failed and OPENAI_API_KEY is not configured"
                )

            rubric_rows = (
                sb.table("mini_check_rubrics")
                .select("problem,rubric")
                .eq("quiz_id", quiz_key)
                .limit(1)
                .execute()
                .data
            )

            exam_file = load_exam_file(quiz_key)

            if rubric_rows:
                rubric_row = rubric_rows[0]
                problem = (
                    rubric_row.get("problem", "")
                    or "Use the attached official exam/questions file."
                )
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

        feedback = "העבודה התקבלה, אך הבדיקה האוטומטית נכשלה. המרצה יכול להפעיל בדיקה חוזרת."

        try:
            sb.table("mini_check_submissions").update({
                "status": "error",
                "score": None,
                "feedback": feedback,
            }).eq("id", submission_id).execute()
        except Exception as update_error:
            print("AUTO GRADE STATUS UPDATE ERROR:", update_error)

        return {
            "status": "error",
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

    try:
        identity = student_access_identity(code, pin)
        if not identity:
            return jsonify(error="קוד סטודנט או PIN אינם רשומים"), 403
    except Exception as e:
        print("STUDENT ACCESS ERROR:", e)
        return jsonify(error="שגיאה באימות פרטי הסטודנט"), 500

    canonical_code = identity["student_code"]
    access_pin_hash = identity["pin_hash"]

    allowed = {".png", ".jpg", ".jpeg"}

    for f in files:
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in allowed:
            return jsonify(error="ניתן להעלות רק קבצי PNG או JPG"), 400

    quiz_key = make_quiz_key(course_id, group_id, quiz_id)

    try:
        existing = (
            sb.table("mini_check_submissions")
            .select("id")
            .eq("pin_hash", access_pin_hash)
            .eq("quiz_id", quiz_key)
            .limit(1)
            .execute()
            .data
        )
        if existing:
            return jsonify(error="העבודה הזאת כבר הוגשה"), 409
    except Exception as e:
        print("DUPLICATE CHECK ERROR:", e)
        return jsonify(error="שגיאה בבדיקת הגשה קיימת"), 500

    sid = str(uuid.uuid4())
    uploaded_paths = []

    try:
        for index, f in enumerate(files, start=1):
            ext = os.path.splitext(f.filename)[1].lower()

            # Keep storage path simple and independent of Hebrew/course names.
            path = f"{canonical_code}/{sid}/{index:03d}{ext}"
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
            "student_code": canonical_code,
            "pin_hash": access_pin_hash,
            "quiz_id": quiz_key,
            "file_path": json.dumps(uploaded_paths, ensure_ascii=False),
            "status": "submitted"
        }).execute()

        # The student page starts AI grading immediately after this response.
        return jsonify(
            submission_id=sid,
            files=len(uploaded_paths),
            course_id=course_id,
            group_id=group_id,
            quiz_id=quiz_id,
            status="submitted",
            score=None,
            feedback="העבודה התקבלה. הבדיקה האוטומטית מתחילה."
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

    try:
        identity = student_access_identity(code, pin)
        if not identity:
            return jsonify(error="קוד סטודנט או PIN אינם רשומים"), 403
    except Exception as e:
        print("STUDENT ACCESS ERROR:", e)
        return jsonify(error="שגיאה באימות פרטי הסטודנט"), 500

    access_pin_hash = identity["pin_hash"]

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
            .eq("pin_hash", access_pin_hash)
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

    try:
        identity = student_access_identity(code, pin)
        if not identity:
            return jsonify(error="קוד סטודנט או PIN אינם רשומים"), 403
    except Exception as e:
        print("STUDENT ACCESS ERROR:", e)
        return jsonify(error="שגיאה באימות פרטי הסטודנט"), 500

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

    if (
        normalize_student_code(row.get("student_code"))
        != identity["student_code"]
        or row.get("pin_hash") != identity["pin_hash"]
    ):
        return jsonify(error="קוד סטודנט או PIN שגויים"), 403

    current_status = row.get("status") or "submitted"

    if current_status == "graded" and row.get("score") is not None:
        return jsonify(
            submission_id=submission_id,
            status="graded",
            score=row.get("score"),
            feedback=row.get("feedback") or ""
        )

    # A student gets only one AI attempt per submission.  Returning the
    # stored result here avoids another paid OpenAI request after an
    # uncertain result, a technical failure, or a repeated button click.
    if current_status != "submitted":
        return jsonify(
            submission_id=submission_id,
            status=current_status,
            score=row.get("score"),
            feedback=row.get("feedback") or "הבדיקה כבר הופעלה עבור הגשה זו."
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
        # Claim the submission atomically.  The status condition also blocks
        # two near-simultaneous requests from starting two paid AI checks.
        claim = sb.table("mini_check_submissions").update({
            "status": "grading",
            "score": None
        }).eq("id", submission_id).eq("status", "submitted").execute()

        if not claim.data:
            latest_rows = (
                sb.table("mini_check_submissions")
                .select("status,score,feedback")
                .eq("id", submission_id)
                .limit(1)
                .execute()
                .data
            )
            latest = latest_rows[0] if latest_rows else {}
            return jsonify(
                submission_id=submission_id,
                status=latest.get("status") or "grading",
                score=latest.get("score"),
                feedback=latest.get("feedback") or "הבדיקה כבר הופעלה עבור הגשה זו."
            )

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

    try:
        identity = student_access_identity(code, pin)
        if not identity:
            return jsonify(error="קוד סטודנט או PIN אינם רשומים"), 403
    except Exception as e:
        print("STUDENT ACCESS ERROR:", e)
        return jsonify(error="שגיאה באימות פרטי הסטודנט"), 500

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
        normalize_student_code(row.get("student_code"))
        != identity["student_code"]
        or row.get("pin_hash") != identity["pin_hash"]
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
    pin = d.get("pin", "")

    try:
        identity = student_access_identity(code, pin)
        if not identity:
            return jsonify(error="קוד סטודנט או PIN אינם רשומים"), 403
    except Exception as e:
        print("STUDENT ACCESS ERROR:", e)
        return jsonify(error="שגיאה באימות פרטי הסטודנט"), 500

    rows = (
        sb.table("mini_check_submissions")
        .select(
            "quiz_id,"
            "score,"
            "feedback,"
            "status,"
            "created_at"
        )
        .eq("pin_hash", identity["pin_hash"])
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
# TEACHER – EXPORT GRADES TO EXCEL
# ==========================================================

@app.post("/teacher/export.xlsx")
def teacher_export_xlsx():
    d = request.get_json(force=True)
    password = str(d.get("teacher_password", "")).strip()

    if not teacher_password_ok(password):
        return jsonify(error="Invalid teacher password"), 403

    selected_course = str(d.get("course_id", "ALL")).strip() or "ALL"
    selected_quiz = str(d.get("quiz_id", "ALL")).strip() or "ALL"
    selected_status = str(d.get("status", "ALL")).strip() or "ALL"

    try:
        rows = (
            sb.table("mini_check_submissions")
            .select(
                "student_code,"
                "quiz_id,"
                "score,"
                "feedback,"
                "status,"
                "created_at"
            )
            .order("created_at", desc=True)
            .execute()
            .data
        )

        records = grouped_submission_rows(rows)
        filtered = []

        for record in records:
            if (
                selected_course != "ALL"
                and record.get("course_id") != selected_course
            ):
                continue
            if (
                selected_quiz != "ALL"
                and str(record.get("quiz_id", "")) != selected_quiz
            ):
                continue
            if selected_status == "UNGRADED":
                if not record.get("has_no_score"):
                    continue
            elif (
                selected_status != "ALL"
                and record.get("status") != selected_status
            ):
                continue
            filtered.append(record)

        filtered.sort(key=lambda item: (
            str(item.get("student_code", "")),
            str(item.get("quiz_id", "")),
        ))

        workbook = build_grades_xlsx(filtered)

        filename_parts = ["grades"]
        if selected_course != "ALL":
            filename_parts.append(selected_course)
        if selected_quiz != "ALL":
            filename_parts.append(selected_quiz)
        filename_parts.append(datetime.now(timezone.utc).date().isoformat())
        filename = "_".join(
            re.sub(r"[^A-Za-z0-9_-]+", "-", part).strip("-") or "all"
            for part in filename_parts
        ) + ".xlsx"

        response = send_file(
            workbook,
            mimetype=(
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
            as_attachment=True,
            download_name=filename,
            max_age=0,
        )
        response.headers["X-Exported-Rows"] = str(len(filtered))
        return response

    except Exception as e:
        print("TEACHER EXCEL EXPORT ERROR:", e, flush=True)
        return jsonify(error="Excel export failed"), 500


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


@app.post("/teacher/students")
def teacher_students():
    d = request.get_json(force=True)
    password = str(d.get("teacher_password", "")).strip()
    action = str(d.get("action", "list")).strip().lower()

    if not teacher_password_ok(password):
        return jsonify(error="Invalid teacher password"), 403

    if action == "list":
        rows = (
            sb.table("mini_check_student_access")
            .select("student_code,active,created_at")
            .order("student_code")
            .execute()
            .data
        )

        grouped = {}
        for row in rows:
            code = normalize_student_code(row.get("student_code"))
            if not code:
                continue
            item = grouped.setdefault(code, {
                "student_code": code,
                "active": False,
                "registered_pairs": 0,
            })
            item["registered_pairs"] += 1
            item["active"] = item["active"] or bool(row.get("active"))

        return jsonify(students=list(grouped.values()))

    raw_code = str(d.get("student_code", "")).strip()
    code = normalize_student_code(raw_code)
    if not code:
        return jsonify(error="Missing student code"), 400

    if action in {"add", "reset_pin"}:
        pin = str(d.get("pin", "")).strip()
        if len(pin) < 4:
            return jsonify(error="PIN must contain at least 4 characters"), 400

        rows = (
            sb.table("mini_check_student_access")
            .select("student_code,pin_hash,active")
            .execute()
            .data
        )
        matching_rows = [
            row for row in rows
            if normalize_student_code(row.get("student_code")) == code
        ]

        if action == "add" and matching_rows:
            same_pair_rows = [
                row for row in matching_rows
                if str(row.get("pin_hash", ""))
                == phash(str(row.get("student_code", "")).strip(), pin)
            ]
            if same_pair_rows:
                for row in same_pair_rows:
                    sb.table("mini_check_student_access") \
                        .update({"active": True}) \
                        .eq(
                            "student_code",
                            str(row.get("student_code", "")).strip()
                        ) \
                        .eq("pin_hash", row.get("pin_hash")) \
                        .execute()
                return jsonify(ok=True, student_code=code, active=True)
            return jsonify(
                error="Student code already exists; use reset PIN"
            ), 409

        if action == "reset_pin":
            active_hashes = {
                str(row.get("pin_hash", ""))
                for row in matching_rows
                if bool(row.get("active")) and row.get("pin_hash")
            }
            if len(active_hashes) > 1:
                return jsonify(
                    error=(
                        "This code has multiple active PINs. "
                        "Assign a new unique student code first."
                    )
                ), 409

            matching_codes = {
                str(row.get("student_code", "")).strip()
                for row in matching_rows
            }

            new_hash = phash(code, pin)
            old_hashes = {
                str(row.get("pin_hash", ""))
                for row in matching_rows
                if row.get("pin_hash")
            }
            for old_hash in old_hashes:
                sb.table("mini_check_submissions") \
                    .update({
                        "student_code": code,
                        "pin_hash": new_hash,
                    }) \
                    .eq("pin_hash", old_hash) \
                    .execute()

            for stored_code in matching_codes:
                sb.table("mini_check_student_access") \
                    .delete() \
                    .eq("student_code", stored_code) \
                    .execute()

        sb.table("mini_check_student_access").upsert({
            "student_code": code,
            "pin_hash": phash(code, pin),
            "active": True,
        }, on_conflict="student_code,pin_hash").execute()

        return jsonify(ok=True, student_code=code, active=True)

    if action == "set_active":
        active = bool(d.get("active"))
        rows = (
            sb.table("mini_check_student_access")
            .select("student_code")
            .execute()
            .data
        )
        matching_codes = {
            str(row.get("student_code", "")).strip()
            for row in rows
            if normalize_student_code(row.get("student_code")) == code
        }
        if not matching_codes:
            return jsonify(error="Student code not found"), 404

        for stored_code in matching_codes:
            sb.table("mini_check_student_access") \
                .update({"active": active}) \
                .eq("student_code", stored_code) \
                .execute()

        return jsonify(ok=True, student_code=code, active=active)

    return jsonify(error="Unknown action"), 400


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
