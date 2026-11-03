$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonExecutable = "D:\python\python.exe"

if (-not (Test-Path -LiteralPath $pythonExecutable -PathType Leaf)) {
    throw "未找到网站运行环境：$pythonExecutable"
}

$hostName = if ($env:WEB_HOST) { $env:WEB_HOST } else { "127.0.0.1" }
$port = if ($env:WEB_PORT) { $env:WEB_PORT } else { "8000" }
Set-Location -LiteralPath $projectRoot
Write-Host "使用 Python：$pythonExecutable"
Write-Host "启动地址：http://${hostName}:$port"
& $pythonExecutable -m uvicorn api:app --host $hostName --port $port
