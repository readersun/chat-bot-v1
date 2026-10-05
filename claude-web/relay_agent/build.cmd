@echo off
rem ===========================================================================
rem  relay.exe 만들기 (관리자가 한 번)
rem
rem  이 파일을 윈도우에서 더블클릭하거나 cmd 에서 실행한다.
rem  나온 dist\relay.exe 를 웹의 [운영 - 중계 설정 - 중계 프로그램] 에 올리면
rem  그 뒤로는 쓰는 사람이 웹에서 직접 받아 간다.
rem
rem  필요한 것
rem    - 파이썬 3.8 이상 (python.org 설치본이면 된다)
rem    - pyinstaller  (아래에서 자동으로 깐다. 인터넷이 되는 PC 에서 한다)
rem
rem  왜 exe 로 묶는가
rem    VDI 에 파이썬이 깔려 있지 않다. relay.py 는 표준 라이브러리만 쓰므로
rem    파이썬이 있는 PC 라면 `python relay.py` 로 그냥 돌아간다. 없는 PC 를
rem    위해 한 파일로 묶는다.
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
python -m PyInstaller --onefile --name relay --console ^
    --noconfirm --clean relay.py || (
  echo.
  echo 빌드에 실패했습니다. 위 메시지를 확인하세요.
  pause
  exit /b 1
)

echo.
echo ===========================================================================
echo  다 됐습니다.
echo.
echo    %cd%\dist\relay.exe
echo.
echo  이 파일을 웹에 올리세요.
echo    [운영] - [중계 설정] - [중계 프로그램] - 올리기
echo.
echo  그 다음부터 쓰는 사람은 [서버] 화면의 [내 중계] 에서 직접 받습니다.
echo ===========================================================================
pause
