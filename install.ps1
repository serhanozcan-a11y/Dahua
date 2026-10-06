# Dahua NVR izleme - Windows kurulumu (PowerShell 5.1+)
# Gereksinim: Docker Desktop kurulu ve calisiyor olmali.
# Kullanim:  powershell -ExecutionPolicy Bypass -File .\install.ps1

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

docker compose version *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "HATA: Docker Desktop kurulu degil veya calismiyor." -ForegroundColor Red
    Write-Host "Kurulum: https://www.docker.com/products/docker-desktop/"
    exit 1
}

function New-RandomHex([int]$byteCount) {
    $bytes = New-Object byte[] $byteCount
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    return (($bytes | ForEach-Object { $_.ToString("x2") }) -join "")
}

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

if (-not (Test-Path ".env")) {
    $dbPw     = New-RandomHex 16
    $grafPw   = New-RandomHex 8
    $panelPw  = New-RandomHex 8
    $keyBytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($keyBytes)
    $secret   = [Convert]::ToBase64String($keyBytes).Replace('+','-').Replace('/','_')

    $envText = "DB_PASSWORD=$dbPw`nGRAFANA_PASSWORD=$grafPw`nPANEL_PASSWORD=$panelPw`nSECRET_KEY=$secret`n"
    [System.IO.File]::WriteAllText((Join-Path $PSScriptRoot ".env"), $envText, $utf8NoBom)

    Write-Host "==> .env uretildi." -ForegroundColor Green
    Write-Host "    Grafana : admin / $grafPw"
    Write-Host "    Panel   : admin / $panelPw"
    Write-Host "    Bu parolalari guvenli bir yere kaydedin." -ForegroundColor Yellow
} else {
    Write-Host "==> Mevcut .env korunuyor."
}

if (-not (Test-Path "devices.yaml")) {
    [System.IO.File]::WriteAllText((Join-Path $PSScriptRoot "devices.yaml"), "", $utf8NoBom)
    Write-Host "==> Bos devices.yaml olusturuldu (cihazlari panelden ekleyin)."
}

docker compose up -d --build
if ($LASTEXITCODE -ne 0) {
    Write-Host "HATA: docker compose basarisiz oldu. Yukaridaki hatayi kontrol edin." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "==> Kurulum tamam." -ForegroundColor Green
Write-Host "    Panel (NVR ekleme):  http://localhost:8000   (admin / .env: PANEL_PASSWORD)"
Write-Host "    Grafana panosu:      http://localhost:3000   (admin / .env: GRAFANA_PASSWORD)"
Write-Host "    Cihaz ekledikten sonra:  docker compose restart collector"
