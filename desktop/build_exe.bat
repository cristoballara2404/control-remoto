@echo off
REM Genera dist\ControlRemoto\ControlRemoto.exe (carpeta lista para el instalador)
REM Ejecutar desde esta carpeta, en Windows, con Python 3.10 o superior.
REM (Se llama con "python -m PyInstaller" porque con Python de la Microsoft Store
REM  el comando "pyinstaller" suele no estar en el PATH.)
setlocal

python -m pip install --upgrade pip || goto :error
python -m pip install -r requirements.txt pyinstaller || goto :error

python -c "from icons import make_icon; make_icon('#44bd50', 256).save('icon.ico', sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])" || goto :error

python -m PyInstaller --noconfirm --clean --onedir --windowed ^
  --name ControlRemoto --icon icon.ico ^
  --collect-all customtkinter --collect-all zeroconf --collect-all soundcard --collect-all pystray ^
  --collect-submodules pynput --collect-submodules mss --collect-submodules websockets ^
  --hidden-import PIL._tkinter_finder --hidden-import _cffi_backend ^
  app.py || goto :error

echo.
echo Listo: dist\ControlRemoto\ControlRemoto.exe
echo Para crear el instalador, abre installer.iss con Inno Setup y presiona Compilar.
exit /b 0

:error
echo.
echo Algo fallo. Copia el mensaje de arriba y pegalo en el chat.
exit /b 1
