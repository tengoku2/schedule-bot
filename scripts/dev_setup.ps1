$ErrorActionPreference = "Stop"

if (-not (Test-Path ".venv")) {
    python -m venv .venv
}

. .\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host ".env created. Set TOKEN and DB connection values before running the bot."
}

Write-Host "Setup complete"
Write-Host "Activate venv: .\\.venv\\Scripts\\Activate.ps1"
Write-Host "Run validation: python scripts/check_env.py"
Write-Host "Run tests: pytest"
