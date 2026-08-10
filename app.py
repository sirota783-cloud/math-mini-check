import os, hashlib, uuid
from flask import Flask, request, jsonify
from flask_cors import CORS
from supabase import create_client

app=Flask(__name__)
CORS(app)

sb=create_client(os.environ["SUPABASE_URL"],os.environ["SUPABASE_SERVICE_KEY"])
TEACHER_KEY=os.environ["TEACHER_KEY"]

def phash(code,pin):
    return hashlib.sha256((code+"|"+pin).encode()).hexdigest()

def teacher_ok():
    return request.headers.get("X-Teacher-Key","")==TEACHER_KEY

@app.get("/health")
def health():
    return {"ok":True}

@app.post("/submit")
def submit():
    code=request.form.get("student_code","").strip()
    pin=request.form.get("pin","")
    quiz_id=request.form.get("quiz_id","").strip()
    f=request.files.get("file")
    if not code or not pin or not quiz_id or not f:
        return jsonify(error="חסרים שדות חובה"),400

    sid=str(uuid.uuid4())
    ext=os.path.splitext(f.filename)[1].lower() or ".bin"
    path=f"{quiz_id}/{sid}{ext}"
    content=f.read()

    sb.storage.from_("submissions").upload(
        path,content,{"content-type":f.mimetype,"upsert":"false"}
    )

    sb.table("submissions").insert({
        "id":sid,
        "student_code":code,
        "pin_hash":phash(code,pin),
        "quiz_id":quiz_id,
        "file_path":path,
        "status":"submitted"
    }).execute()

    return jsonify(submission_id=sid)

@app.post("/results")
def results():
    d=request.get_json(force=True)
    code=d.get("student_code","").strip()
    p=phash(code,d.get("pin",""))

    rows=sb.table("submissions")\
        .select("quiz_id,score,feedback,status,created_at")\
        .eq("student_code",code)\
        .eq("pin_hash",p)\
        .order("created_at",desc=True)\
        .execute().data

    return jsonify(results=rows)

@app.post("/teacher/rubric")
def rubric():
    if not teacher_ok():
        return jsonify(error="Forbidden"),403

    d=request.get_json(force=True)
    sb.table("rubrics").upsert({
        "quiz_id":d["quiz_id"],
        "problem":d.get("problem",""),
        "rubric":d.get("rubric","")
    }).execute()

    return {"ok":True}

@app.get("/teacher/submissions")
def submissions():
    if not teacher_ok():
        return jsonify(error="Forbidden"),403

    rows=sb.table("submissions")\
        .select("id,student_code,quiz_id,score,status,created_at")\
        .order("created_at",desc=True)\
        .execute().data

    return jsonify(submissions=rows)

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT","10000")))
