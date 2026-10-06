@echo off
rem ===========================================================================
rem  claude-term.exe 만들기 (관리자가 한 번)
rem
rem  이 파일을 윈도우에서 더블클릭하거나 cmd 에서 실행한다.
rem  나온 dist\claude-term.exe 와 putty.exe 를 한 폴더에 넣고 zip 으로 묶어
rem  웹의 [운영 - 중계 설정 - 사용자 클라이언트] 에 올린다.
rem
rem  필요한 것
rem    - 파이썬 3.8 이상 (python.org 설치본. tkinter 가 들어 있어야 한다)
rem    - pyinstaller  (아래에서 자동으로 깐다. 인터넷이 되는 PC 에서 한다)
rem ===========================================================================
setlocal
cd /d "%~dp0"

echo [1/3] 파이썬 확인
python --version || (
  echo.
  echo 파이썬을 찾지 못했습니다. python.org 에서 설치한 뒤 다시 실행하세요.
  pause
  exit /b 1
)
python -c "import tkinter" || (
  echo.
  echo tkinter 가 없습니다. python.org 설치본으로 다시 설치하세요. (tcl/tk 포함)
  pause
  exit /b 1
)

echo.
echo [2/3] pyinstaller 준비
python -m pip install --disable-pip-version-check --quiet pyinstaller || (
  echo.
  echo pyinstaller 설치에 실패했습니다. 인터넷이 되는 PC 에서 실행하세요.
  pause
  exit /b 1
)

echo.
echo [3/3] 묶는 중 (1~2분)
python -m PyInstaller --onefile --windowed --name claude-term ^
    --noconfirm --clean claude_term.py || (
  echo.
  echo 빌드에 실패했습니다. 위 메시지를 확인하세요.
  pause
  exit /b 1
)

echo.
echo ===========================================================================
echo  다 됐습니다.
echo.
echo    %cd%\dist\claude-term.exe
echo.
echo  이 파일과 putty.exe 를 **한 폴더에** 넣고 그 둘만 zip 으로 묶어 올리세요.
echo    [운영] - [중계 설정] - [사용자 클라이언트] - 올리기
echo.
echo  쓰는 사람은 [서버] 화면의 [내 클라이언트] 에서 받습니다.
echo ===========================================================================
pause
