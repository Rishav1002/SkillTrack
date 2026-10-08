@echo off
set ROOT=%~dp0..
python -m venv "%ROOT%\.venv"
call "%ROOT%\.venv\Scripts\activate.bat"
pip install -r "%ROOT%\backend\requirements.txt"
cd /d "%ROOT%\frontend"
npm install
npm run build
cd /d "%ROOT%"
echo SkillTrack is ready. Start with: .venv\Scripts\activate && python run_demo.py
