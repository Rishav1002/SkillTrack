# SkillTrack ER diagram

```mermaid
erDiagram
  USERS ||--o| STUDENTS : has
  USERS ||--o| COMPANIES : owns
  STUDENTS ||--o{ STUDENT_SKILLS : confirms
  SKILLS ||--o{ STUDENT_SKILLS : maps
  STUDENTS ||--o{ PROJECTS : builds
  STUDENTS ||--o{ CERTIFICATES : earns
  COMPANIES ||--o{ JOBS : posts
  STUDENTS ||--o{ RESUMES : uploads
  JOBS ||--o{ APPLICATIONS : receives
  STUDENTS ||--o{ APPLICATIONS : submits
  APPLICATIONS ||--o{ HISTORY : records
  USERS ||--o{ NOTIFICATIONS : receives

  USERS { int id PK; string email; string password_hash; string role; boolean is_active }
  STUDENTS { int user_id PK; string full_name; float cgpa; int graduation_year }
  COMPANIES { int id PK; int user_id; string name; boolean is_approved }
  JOBS { int id PK; int company_id; string title; string deadline; boolean is_approved }
  APPLICATIONS { int id PK; int job_id; int student_id; float ats_score; string status }
  SKILLS { int id PK; string name; string category; string aliases }
```
