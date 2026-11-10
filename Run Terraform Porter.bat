@echo off
REM Launches the Air-Gapped Terraform Porter with no console window.
REM pythonw runs without a terminal; falls back to python if needed.
start "" pythonw "%~dp0porter.py" 2>nul || start "" python "%~dp0porter.py"
