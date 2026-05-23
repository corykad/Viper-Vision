#define MyAppName "Viper Vision"
#define MyAppVersion "1.2.3"
#define MyAppPublisher "Cory Kadlec"
#define MyAppExeName "ViperVision.exe"

[Setup]
AppId={{7A6AA4FA-6E7E-4C5D-950D-9C35093F829B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\Viper Vision
DefaultGroupName=Viper Vision
DisableProgramGroupPage=yes
LicenseFile=LICENSE
OutputDir=installer
OutputBaseFilename=ViperVision-v{#MyAppVersion}-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
SetupLogging=yes
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "dist\ViperVision\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Viper Vision"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall Viper Vision"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Viper Vision"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch Viper Vision"; Flags: nowait postinstall skipifsilent
