param(
    [ValidateSet('fake','openai','ollama')][string]$Backend = 'openai',
    [int]$Port = 8105
)
$ErrorActionPreference = 'Stop'
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw 'README.md의 가상환경 설치 절차를 먼저 실행하세요.' }
$env:LLM_BACKEND = $Backend
$env:LANGCHAIN_TRACING_V2 = 'false'
$env:LANGSMITH_TRACING = 'false'
$env:PYTHONDONTWRITEBYTECODE = '1'
Push-Location $PSScriptRoot
try {
    & $taskPython -X utf8 -m uvicorn server:app --host 127.0.0.1 --port $Port
} finally {
    Pop-Location
}
