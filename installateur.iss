; Installateur de Forever Addons Manager (Inno Setup), compilé par GitHub Actions.
; Installé pour l'utilisateur seul : aucun droit administrateur, aucune fenêtre de contrôle du compte.
#define Nom "Forever Addons Manager"
#ifndef Version
  #define Version "0.0.0"
#endif

[Setup]
AppId={{7E3A9F52-6C1D-4B8E-9A47-2F0C5D1E8B36}
AppName={#Nom}
AppVersion={#Version}
AppVerName={#Nom} {#Version}
AppPublisher=Aux Ptits Casus
DefaultDirName={localappdata}\Programs\{#Nom}
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
PrivilegesRequired=lowest
OutputDir=build\sortie
OutputBaseFilename=Forever-Addons-Manager-Setup
SetupIconFile=ui\icone.ico
UninstallDisplayIcon={app}\{#Nom}.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=force
RestartApplications=no

[Languages]
Name: "fr"; MessagesFile: "compiler:Languages\French.isl"

[InstallDelete]
; Repart d'un dossier propre : rien d'une ancienne version ne traîne
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "build\dist\{#Nom}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\{#Nom}"; Filename: "{app}\{#Nom}.exe"

[Run]
Filename: "{app}\{#Nom}.exe"; Parameters: "--installation"; Flags: runhidden waituntilterminated
Filename: "{app}\{#Nom}.exe"; Description: "Ouvrir {#Nom}"; Flags: nowait postinstall

[UninstallRun]
Filename: "{app}\{#Nom}.exe"; Parameters: "--desinstallation"; Flags: runhidden waituntilterminated; RunOnceId: "Desinstallation"

[Code]
function InitializeSetup(): Boolean;
begin
  // Mise à jour lancée par l'appli : on lui laisse le temps de se fermer
  if ExpandConstant('{param:MAJ|0}') = '1' then Sleep(7000);
  Result := True;
end;
