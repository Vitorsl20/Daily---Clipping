# Chamado pela tarefa agendada. Atualiza o código (se a pasta veio do git), garante as dependências e roda o
# complemento. Toda a saída vai para logs\complemento-AAAA-MM-DD.log.
$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $Repo
$Logs = Join-Path $Repo "logs"
New-Item -ItemType Directory -Force -Path $Logs | Out-Null
$Log = Join-Path $Logs ("complemento-" + (Get-Date -Format "yyyy-MM-dd") + ".log")
"==== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ====" | Out-File -Append -Encoding utf8 $Log
$VenvPy = Join-Path $Repo ".venv\Scripts\python.exe"
if ((Test-Path (Join-Path $Repo ".git")) -and (Get-Command git -ErrorAction SilentlyContinue)) {
    git pull --ff-only --quiet 2>&1 | Out-File -Append -Encoding utf8 $Log
    & $VenvPy -m pip install -r requirements.txt --quiet --disable-pip-version-check 2>&1 | Out-File -Append -Encoding utf8 $Log
}
$env:PYTHONIOENCODING = "utf-8"
& $VenvPy -m clipping --complement 2>&1 | Out-File -Append -Encoding utf8 $Log
