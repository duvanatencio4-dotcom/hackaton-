@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creando entorno virtual...
    py -m venv .venv
)

call .venv\Scripts\activate.bat
python -m pip install --disable-pip-version-check -r requirements.txt
streamlit run app.py

endlocal
