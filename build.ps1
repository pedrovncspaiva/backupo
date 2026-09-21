# Builds the .exe.  Run from the project root:
#
#     .\build.ps1
#     .\build.ps1 -OneFile      # a single self-extracting .exe instead
#     .\build.ps1 -SkipTests    # skip the test run first
#
# The result is dist\backupov2\ - ship that whole folder, or make a shortcut
# to the .exe inside it. With -OneFile the result is a single .exe in dist\,
# which is easier to hand over but starts slower and is more likely to be
# quarantined by antivirus (see "Gerando o executavel" in the README).

param(
    [switch]$OneFile,
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not $SkipTests) {
    Write-Host "Rodando os testes..." -ForegroundColor Cyan
    python -m unittest discover -s tests -t . -q
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Testes falharam - build cancelado." -ForegroundColor Red
        exit 1
    }
}

# The spec reads this; it defaults to the one-folder build.
$env:BACKUPOV2_ONEFILE = if ($OneFile) { "1" } else { "0" }

Write-Host "Empacotando..." -ForegroundColor Cyan
python -m PyInstaller backupov2.spec --noconfirm --clean
if ($LASTEXITCODE -ne 0) { exit 1 }

$target = if ($OneFile) {
    "dist\Assistente de Backup de Discos.exe"
} else {
    "dist\backupov2\Assistente de Backup de Discos.exe"
}

if (Test-Path $target) {
    $size = [math]::Round((Get-Item $target).Length / 1MB, 1)
    Write-Host ""
    Write-Host "Pronto: $target  ($size MB)" -ForegroundColor Green
} else {
    # A build that "succeeded" with nothing at the end of it is almost always
    # the antivirus, not PyInstaller.
    Write-Host ""
    Write-Host "O build terminou mas o arquivo nao esta la: $target" -ForegroundColor Yellow
    Write-Host "Isso normalmente e o antivirus removendo o .exe assim que ele aparece." -ForegroundColor Yellow
    Write-Host "Veja 'Gerando o executavel' no README." -ForegroundColor Yellow
    exit 1
}
