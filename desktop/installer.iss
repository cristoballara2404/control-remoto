; Instalador de Control Remoto (Inno Setup 6: https://jrsoftware.org/isinfo.php)
;
; Carpeta de trabajo esperada (ejemplo):
;   C:\ruta\a\control-remoto\desktop\
;       installer.iss      <- este archivo
;       build_exe.bat
;       dist\ControlRemoto\ControlRemoto.exe   <- lo crea build_exe.bat
;       icon.ico                                <- lo crea build_exe.bat
;
; Pasos: 1) ejecuta build_exe.bat   2) abre este archivo con Inno Setup   3) Compilar (Ctrl+F9)
; Resultado: Output\ControlRemoto-Setup.exe (dentro de la misma carpeta app-pc)

#define Root AddBackslash(SourcePath)

#if !FileExists(Root + "dist\ControlRemoto\ControlRemoto.exe")
  #error Falta dist\ControlRemoto\ControlRemoto.exe. Primero ejecuta build_exe.bat en la misma carpeta que este archivo.
#endif
#if !FileExists(Root + "icon.ico")
  #error Falta icon.ico. Lo crea build_exe.bat: ejecutalo primero.
#endif

[Setup]
AppId={{B7C3C6A2-5E0E-4C57-9B0E-7A1D9E6F4C21}
AppName=Control Remoto
AppVersion=1.0.0
AppPublisher=SopitaD
DefaultDirName={autopf}\ControlRemoto
DefaultGroupName=Control Remoto
DisableProgramGroupPage=yes
OutputDir={#Root}Output
OutputBaseFilename=ControlRemoto-Setup
SetupIconFile={#Root}icon.ico
UninstallDisplayIcon={app}\ControlRemoto.exe
Compression=lzma2
SolidCompression=yes
; Hace falta administrador para crear la regla del firewall
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
; Avisa si Control Remoto está abierto al instalar o desinstalar (la app crea este mutex)
AppMutex=ControlRemoto_SingleInstance

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"

[Tasks]
Name: "desktopicon"; Description: "Crear un acceso directo en el escritorio"; GroupDescription: "Accesos directos:"

[Files]
Source: "{#Root}dist\ControlRemoto\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Control Remoto"; Filename: "{app}\ControlRemoto.exe"
Name: "{autodesktop}\Control Remoto"; Filename: "{app}\ControlRemoto.exe"; Tasks: desktopicon

[Run]
; Permite las conexiones entrantes del celular solo en redes privadas (casa, trabajo)
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Control Remoto"""; Flags: runhidden
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Control Remoto"" dir=in action=allow program=""{app}\ControlRemoto.exe"" enable=yes profile=private"; Flags: runhidden
Filename: "{app}\ControlRemoto.exe"; Description: "Abrir Control Remoto"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Control Remoto"""; Flags: runhidden

; Nota: al desinstalar NO se borra %APPDATA%\ControlRemoto (dispositivos vinculados y opciones),
; así al reinstalar tus celulares siguen vinculados.
