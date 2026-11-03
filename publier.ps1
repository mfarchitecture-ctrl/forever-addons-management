# Publie la version écrite dans hamax.py : envoie le code et pose l'étiquette, GitHub fait le reste
# (voir .github/workflows/publication.yml). Les utilisateurs voient ensuite la mise à jour dans l'appli.
#   powershell -ExecutionPolicy Bypass -File publier.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$version = (Select-String -Path hamax.py -Pattern '^VERSION = "([\d.]+)"').Matches[0].Groups[1].Value
if (-not (Select-String -Path CHANGELOG.md -SimpleMatch "## $version " -Quiet)) { throw "CHANGELOG.md n'a pas de section $version." }
if (git tag --list "v$version") { throw "La version $version est déjà publiée : monte VERSION dans hamax.py." }

git add -A
git diff --cached --quiet
if ($LASTEXITCODE -ne 0) { git commit -m "Version $version" }
git push origin main
if ($LASTEXITCODE -ne 0) { throw "Envoi sur GitHub refusé." }
git tag "v$version"
git push origin "v$version"
if ($LASTEXITCODE -ne 0) { throw "Envoi de l'étiquette refusé." }
Write-Host "Version $version envoyée : https://github.com/mfarchitecture-ctrl/forever-addons-management/actions"
