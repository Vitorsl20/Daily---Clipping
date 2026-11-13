#Requires -Version 5.1
# Instala o complemento das 6h do LatAm Financials Clipping neste computador (Windows).
# Pode rodar de novo quantas vezes quiser: ele atualiza tudo e recria a tarefa agendada.
$ErrorActionPreference = "Stop"
$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Write-Host ""
Write-Host "== LatAm Financials Clipping - instalação do complemento das 6h ==" -ForegroundColor Cyan
Write-Host "Pasta do projeto: $Repo"

# 1) Python 3.11+
$PyExe = $null; $PyArgs = @()
if (Get-Command py -ErrorAction SilentlyContinue) { $PyExe = "py"; $PyArgs = @("-3") }
elseif ((Get-Command python -ErrorAction SilentlyContinue) -and -not ((Get-Command python).Source -like "*WindowsApps*")) { $PyExe = "python" }
if (-not $PyExe) {
    Write-Host "Python não encontrado. Instalando o Python 3.12 pelo winget..." -ForegroundColor Yellow
    winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    Write-Host "Pronto. FECHE esta janela e rode o instalador de novo." -ForegroundColor Yellow
    exit 1
}
& $PyExe @PyArgs -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if ($LASTEXITCODE -ne 0) { throw "É preciso Python 3.11 ou mais novo (winget install Python.Python.3.12)." }

# 2) Ambiente virtual + dependências
$Venv = Join-Path $Repo ".venv"
$VenvPy = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $VenvPy)) {
    Write-Host "Criando ambiente Python..."
    & $PyExe @PyArgs -m venv $Venv
}
Write-Host "Instalando dependências (pode levar 1-2 minutos)..."
& $VenvPy -m pip install --upgrade pip --quiet --disable-pip-version-check
& $VenvPy -m pip install -r (Join-Path $Repo "requirements.txt") --quiet --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { throw "Falha ao instalar as dependências." }

# 3) Credenciais (ficam só neste computador, no arquivo .env, legível apenas pelo seu usuário)
function Read-Secret([string]$Prompt) {
    $sec = Read-Host $Prompt -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}
$EnvFile = Join-Path $Repo ".env"
$write = $true
if (Test-Path $EnvFile) { $write = (Read-Host "Já existem credenciais salvas. Digitar de novo? (s/N)") -match "^[sS]" }
if ($write) {
    Write-Host ""
    Write-Host "Use as MESMAS credenciais cadastradas nos Secrets do GitHub." -ForegroundColor Cyan
    $key = Read-Secret "Chave da API do Claude (ANTHROPIC_API_KEY)"
    $gm  = Read-Host "Gmail que envia o clipping (GMAIL_ADDRESS)"
    $gp  = (Read-Secret "Senha de app do Gmail - 16 letras (GMAIL_APP_PASSWORD)") -replace "\s", ""
    $to  = Read-Host "E-mail que recebe (CLIPPING_TO) - Enter para usar o mesmo"
    if (-not $to) { $to = $gm }
    $content = "ANTHROPIC_API_KEY=$key`nGMAIL_ADDRESS=$gm`nGMAIL_APP_PASSWORD=$gp`nCLIPPING_TO=$to`n"
    [IO.File]::WriteAllText($EnvFile, $content, (New-Object System.Text.UTF8Encoding $false))
    icacls $EnvFile /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null
    Write-Host "Credenciais salvas em $EnvFile"
}

# 4) Tarefa agendada: ao fazer login (2 min depois) e às 06:05; se o PC estiver desligado/dormindo às 06:05,
#    roda assim que ele voltar. O próprio programa garante que só roda uma vez por dia útil.
$TaskName = "LatAm Clipping - complemento 6h"
$Runner = Join-Path $PSScriptRoot "rodar_complemento.ps1"
$User = "$env:USERDOMAIN\$env:USERNAME"
$Action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Runner`""
$AtLogon = New-ScheduledTaskTrigger -AtLogOn -User $User
$AtLogon.Delay = "PT2M"
$Daily = New-ScheduledTaskTrigger -Daily -At "06:05"
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1) -MultipleInstances IgnoreNew
$Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
try {
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger @($AtLogon, $Daily) -Settings $Settings `
        -Principal $Principal -Force -ErrorAction Stop | Out-Null
} catch {
    # Algumas configurações do Windows só deixam criar gatilho "ao fazer login" como administrador.
    # O gatilho das 06:05 com "rodar assim que possível se perdido" já cobre o PC ligado às 6h.
    Write-Host "Aviso: gatilho de login não permitido sem administrador; usando só o das 06:05." -ForegroundColor Yellow
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Daily -Settings $Settings `
        -Principal $Principal -Force -ErrorAction Stop | Out-Null
}
Write-Host ""
Write-Host "Tarefa agendada criada: '$TaskName'" -ForegroundColor Green
Write-Host "Logs de cada dia ficam em: $(Join-Path $Repo 'logs')"

# 5) Teste opcional (não envia e-mail)
if ((Read-Host "Fazer um teste agora, sem enviar e-mail? Leva alguns minutos (S/n)") -notmatch "^[nN]") {
    Push-Location $Repo
    $env:PYTHONIOENCODING = "utf-8"
    & $VenvPy -m clipping --complement --dry-run --force
    Pop-Location
    Write-Host ""
    Write-Host "Teste concluído. O resultado também ficou na pasta output\." -ForegroundColor Green
}
