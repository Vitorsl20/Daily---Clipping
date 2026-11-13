# Remove a tarefa agendada do complemento (o código e as credenciais em .env continuam na pasta).
Unregister-ScheduledTask -TaskName "LatAm Clipping - complemento 6h" -Confirm:$false -ErrorAction SilentlyContinue
Write-Host "Tarefa removida. Para apagar as credenciais, apague o arquivo .env da pasta do projeto."
