# Viva preparation

1. **Why JWT?** Stateless access control with short-lived access tokens and refresh rotation.
2. **Why bcrypt?** Salted, deliberately slow password hashing that resists offline cracking.
3. **Why TF-IDF?** Transparent lexical baseline that works offline and is easy to audit.
4. **Why embeddings?** Better semantic similarity when the optional model is available; method is returned honestly.
5. **Why hybrid weights?** Skills and eligibility keep role constraints explicit while semantic similarity captures related phrasing.
6. **What is synthetic?** ML evaluation pairs and placement rows are generated reproducibly and labelled.
7. **What are evaluation limits?** Synthetic labels are not real hiring outcomes; ranking metrics measure a narrow task.
8. **What does blind mode hide?** Name, email, gender, college, avatar and free-text identity signals on the server.
9. **Why SQL instead of NoSQL?** Applications, status history, ownership and approval workflows are relational.
10. **How are role checks enforced?** JWT role claims plus ownership queries on every recruiter/student/admin route.
11. **How is resume parsing validated?** PDF magic bytes, size limit, PyMuPDF extraction and empty/scanned-PDF error.
12. **What happens when a score is unavailable?** The application remains `Scoring...`; a background task writes the breakdown.
13. **How are KPIs produced?** Direct database queries, not frontend constants.
14. **What would scale first?** Shared rate-limit storage, Postgres/SQLAlchemy migration and a worker queue.
15. **What is the fairness stance?** Decision support, not auto-rejection; reviewers can inspect matched/missing evidence.
