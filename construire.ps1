# Construit "Forever Addons Manager.exe" dans app\ à partir de hamax.py.
#   powershell -ExecutionPolicy Bypass -File construire.ps1
# Avec -Publication (GitHub Actions) : compile seulement, dans build\dist, sans toucher à ce PC.
# Ferme l'appli s'il tourne, reconstruit, remplace app\, met à jour les raccourcis, relance (près de l'horloge).
param([switch]$Publication)
$ErrorActionPreference = "Stop"
$racine = $PSScriptRoot
$nom = "Forever Addons Manager"
$py = if ($Publication) { "python" } else { Join-Path $racine ".venv\Scripts\python.exe" }
if (-not $Publication -and -not (Test-Path $py)) {
    throw "Environnement manquant : python -m venv .venv puis .venv\Scripts\pip install -r requirements.txt"
}

# Version lue dans hamax.py (VERSION = "x.y.z")
$version = (Select-String -Path (Join-Path $racine "hamax.py") -Pattern '^VERSION = "([\d.]+)"').Matches[0].Groups[1].Value
$v = ($version.Split(".") + @("0", "0", "0"))[0..3] -join ", "
Write-Host "Construction de $nom $version"

# Ressource de version de l'exe (nom affiché dans le Gestionnaire des tâches, Propriétés > Détails)
$build = Join-Path $racine "build"
New-Item -ItemType Directory -Force $build | Out-Null
$infoVersion = Join-Path $build "version_info.txt"
@"
VSVersionInfo(
  ffi=FixedFileInfo(filevers=($v), prodvers=($v), mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040C04B0', [
      StringStruct('CompanyName', '$nom'),
      StringStruct('FileDescription', '$nom'),
      StringStruct('FileVersion', '$version'),
      StringStruct('ProductName', '$nom'),
      StringStruct('ProductVersion', '$version'),
      StringStruct('OriginalFilename', '$nom.exe')])]),
    VarFileInfo([VarStruct('Translation', [0x040C, 1200])])
  ]
)
"@ | Set-Content -Encoding UTF8 $infoVersion

# Fermer l'instance en cours (sinon app\ est verrouillé)
if (-not $Publication) { try {
    Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8765/api/quitter" -Headers @{ "X-Hamax" = "1" } -ContentType "application/json" -Body "{}" -TimeoutSec 3 | Out-Null
    Write-Host "l'appli fermé, attente de l'arrêt..."
    Start-Sleep -Seconds 6
} catch { } }

& $py -m PyInstaller --noconfirm --clean --onedir --windowed `
    --name $nom `
    --icon (Join-Path $racine "ui\icone.ico") `
    --version-file $infoVersion `
    --add-data "$(Join-Path $racine 'ui');ui" `
    --add-data "$(Join-Path $racine 'CHANGELOG.md');." `
    --distpath (Join-Path $build "dist") `
    --workpath (Join-Path $build "travail") `
    --specpath $build `
    (Join-Path $racine "hamax.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller a échoué" }
if ($Publication) { return }

# Remplacer app\ par la nouvelle version
$app = Join-Path $racine "app"
if (Test-Path $app) { Remove-Item -Recurse -Force $app }
Copy-Item -Recurse (Join-Path $build "dist\$nom") $app
$exe = Join-Path $app "$nom.exe"
Write-Host "Prêt : $exe"

# Menu Démarrer de ce PC (pour les membres, ce sera le rôle de l'installateur)
$menu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
foreach ($ancien in @("Startup\Hamax Addons Management.lnk", "Hamax Addons Management.lnk")) {
    Remove-Item (Join-Path $menu $ancien) -ErrorAction SilentlyContinue
}
$s = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $menu "$nom.lnk"))
$s.TargetPath = $exe; $s.WorkingDirectory = $app; $s.Description = $nom; $s.Save()

# Démarrage avec Windows et liens CurseForge vers ce nouvel exe
Start-Process -FilePath $exe -ArgumentList "--installation" -Wait
Write-Host "Raccourcis mis à jour."

# Relancer, discret (icône près de l'horloge)
Start-Process -FilePath $exe -ArgumentList "--demarrage"
Write-Host "$nom $version lancé."
