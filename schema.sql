create table if not exists submissions (
  id uuid primary key,
  student_code text not null,
  pin_hash text not null,
  quiz_id text not null,
  file_path text not null,
  status text not null default 'submitted',
  score numeric,
  feedback text,
  created_at timestamptz not null default now()
);

create index if not exists submissions_student_idx
on submissions(student_code,pin_hash);

create table if not exists rubrics (
  quiz_id text primary key,
  problem text,
  rubric text,
  created_at timestamptz not null default now()
);

-- ב-Supabase Storage יש ליצור bucket פרטי בשם:
-- submissions
