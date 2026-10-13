param(
    [string]$Nome = "Caixa QCM - sincronizacao diaria",
    [string]$Hora = "08:00",
    [switch]$Remover
)
# Cria (ou remove) a tarefa agendada do Windows que roda `python -m caixa.sync --notify` todo dia.
$ErrorActionPreference = "Stop"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $dir ".venv\Scripts\python.exe"

if ($Remover) {
    Unregister-ScheduledTask -TaskName $Nome -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Tarefa '$Nome' removida."
    return
}
if (-not (Test-Path $python)) {
    throw "Ambiente nao encontrado em $python. Abra o iniciar.bat uma vez antes de agendar."
}

$action = New-ScheduledTaskAction -Execute $python -Argument "-m caixa.sync --notify" -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -Daily -At $Hora
# StartWhenAvailable: se o PC estava desligado as 08:00, roda assim que ligar.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName $Nome -Action $action -Trigger $trigger -Settings $settings `
    -Description "Caixa QCM: sincroniza a Conta Simples e avisa se faltar o OFX do PagBank" -Force | Out-Null
Write-Host "Pronto: '$Nome' vai rodar todo dia as $Hora (ou ao ligar o PC, se estava desligado)."
