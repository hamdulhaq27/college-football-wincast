@echo off
title College WinCast Setup
echo ==========================================
echo  College WinCast - Streamlit App
echo ==========================================
echo.

:: Step 1 - Check if virtual environment exists; if not, create with Python 3.11
if not exist venv (
    echo Creating virtual environment using Python 3.11...
    py -3.11 -m venv venv
    if errorlevel 1 (
        echo ----------------------------------------------------------
        echo Python 3.11 is not installed or not registered in the Python Launcher.
        echo Please install Python 3.11 from:
        echo https://www.python.org/downloads/release/python-3110/
        echo ----------------------------------------------------------
        pause
        exit /b
    )
    set FIRST_RUN=1
) else (
    set FIRST_RUN=0
)
echo.

:: Step 2 - Activate venv
echo Activating virtual environment...
call venv\Scripts\activate
if errorlevel 1 (
    echo Failed to activate virtual environment.
    pause
    exit /b
)
echo Environment activated.
echo.

:: Step 3 - Confirm version inside venv
for /f "tokens=2 delims= " %%v in ('python --version') do set VENV_VER=%%v
if not "%VENV_VER:~0,4%"=="3.11" (
    echo ----------------------------------------------------------
    echo The virtual environment is NOT using Python 3.11.
    echo Please delete the "venv" folder and rerun this script.
    echo ----------------------------------------------------------
    pause
    exit /b
)
echo Confirmed: Virtual environment is using Python 3.11 (%VENV_VER%)
echo.

:: Step 4 - First run setup (only if FIRST_RUN=1)
if "%FIRST_RUN%"=="1" (
    echo ------------------------------------------
    echo Performing first-time setup and installation
    echo ------------------------------------------
    echo Upgrading pip...
    python -m pip install --upgrade pip >nul
    echo Pip upgraded successfully.

    echo Cleaning possible local numpy/pandas folders...
    if exist numpy ren numpy _numpy_backup
    if exist pandas ren pandas _pandas_backup
    echo Environment clean.

    echo Installing dependencies from requirements.txt...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo Installation failed. Please check internet connection or permissions.
        pause
        exit /b
    )

    echo Verifying core libraries...
    python -c "import numpy, pandas" 2>nul
    if errorlevel 1 (
        echo NumPy/Pandas import failed. Reinstalling...
        pip install --force-reinstall --upgrade numpy pandas
    )

    echo First-time installation complete.
) else (
    echo ------------------------------------------
    echo Dependencies already installed.
    echo Skipping reinstallation.
    echo ------------------------------------------
)
echo.

:: Step 5 - Kill any running Streamlit processes
echo Checking for running Streamlit processes...
tasklist /FI "IMAGENAME eq streamlit.exe" | find /I "streamlit.exe" >nul
if %ERRORLEVEL%==0 (
    echo Found running Streamlit process. Terminating...
    taskkill /IM streamlit.exe /F >nul 2>&1
    timeout /t 2 >nul
)
echo No conflicting Streamlit processes detected.
echo.

:: Step 6 - Launch Streamlit app
echo Launching College WinCast app (Python 3.11)...
python -m streamlit run main.py

echo.
echo ==========================================
echo  College WinCast is running successfully!
echo ==========================================
pause
