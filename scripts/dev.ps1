#requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory, Position = 0)]
    [ValidateSet('doctor', 'up', 'test', 'down')]
    [string]$Command,

    [Parameter(Position = 1)]
    [ValidateSet('unit', 'integration', 'contract', 'all')]
    [string]$Suite = 'all'
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot '.venv/Scripts/python.exe'
$compose = @('compose', '--project-directory', $repoRoot, '-f', (Join-Path $repoRoot 'compose.yaml'))
$services = @('gateway')

function Test-Python {
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        Write-Host 'Create .venv and install the test dependencies as shown in README.md.'
        return $false
    }
    & $python -I -c 'import sys; import pytest; sys.exit(sys.version_info < (3, 11))' *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'Tests require Python 3.11 or newer and pytest in .venv. See README.md.'
        return $false
    }
    return $true
}

function Test-Compose {
    if (-not (Get-Command docker -CommandType Application -ErrorAction SilentlyContinue)) {
        Write-Host 'Install Docker Desktop with Docker Compose v2.'
        return $false
    }
    & docker compose version *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'Docker Compose v2 is unavailable.'
        return $false
    }
    & docker version --format '{{.Server.Version}}' *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'Cannot reach the Docker daemon. Start Docker Desktop and check access to the active Docker context.'
        return $false
    }
    & docker @compose config --quiet *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'Compose configuration is invalid. Check the Compose file and local .env configuration.'
        return $false
    }
    return $true
}

# Use a temporary source copy because Settings also loads .env beside the source tree.
# Keep the installed venv, but discard the parent application's env and runtime files.
$testRunner = @'
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

repo, suite = Path(sys.argv[1]), sys.argv[2]
directories = ("alembic", "app", "scripts", "tests")
config_files = ("alembic.ini", "pyproject.toml")
ignore_files = shutil.ignore_patterns(
    ".env", ".env.*", "*.env", "*.pem", "*.key", "__pycache__", "*.pyc",
    ".agent_data", "data", "*.db", "*.db-*", "*.sqlite*", "*.log",
)
integration_files = ("tests/integration",)
# These two fixtures start Uvicorn from the real sibling repo on Windows.
excluded_files = (
    "tests/integration/test_full_lifecycle_cross_app.py",
    "tests/integration/test_live_cross_app_e2e.py",
)

selection = {
    "all": ("tests",),
    "contract": ("tests/contract",),
    "integration": integration_files,
    "unit": ("tests",) + tuple(
        f"--ignore={path}" for path in (*integration_files, "tests/contract")
    ),
}

with TemporaryDirectory(prefix="gateway-dev-tests-") as sandbox:
    root = Path(sandbox)
    for directory in directories:
        shutil.copytree(repo / directory, root / directory, ignore=ignore_files)
    for filename in config_files:
        shutil.copy2(repo / filename, root / filename)

    temp = root / "tmp"
    temp.mkdir()
    env = {
        name: os.environ[name]
        for name in ("SystemRoot", "SystemDrive", "WINDIR", "ComSpec", "PATH", "PATHEXT",
                     "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS")
        if name in os.environ
    }
    env.update(TEMP=str(temp), TMP=str(temp), HOME=sandbox, USERPROFILE=sandbox)

    result = subprocess.run(
        [sys.executable, "-I", "-B", "-X", "utf8", "-m", "pytest", "-q", "--tb=short",
         *selection[suite], *(f"--ignore={path}" for path in excluded_files)],
        cwd=root,
        env=env,
        check=False,
    )
sys.exit(result.returncode)
'@

switch ($Command) {
    'doctor' {
        $pythonOk = Test-Python
        $composeOk = Test-Compose
        if ($pythonOk -and $composeOk) {
            Write-Host 'Python, pytest and Docker Compose are ready.'
            exit 0
        }
        exit 1
    }
    'up' {
        if (-not (Test-Compose)) { exit 1 }
        & docker @compose up --build --wait --wait-timeout 120 @services
        $status = $LASTEXITCODE
        if ($status -eq 0) {
            Write-Host 'Gateway is available at http://127.0.0.1:8080/healthz'
        }
        exit $status
    }
    'test' {
        if (-not (Test-Python)) { exit 1 }
        $testRunner | & $python -I -B -X utf8 - $repoRoot $Suite.ToLowerInvariant()
        exit $LASTEXITCODE
    }
    'down' {
        if (-not (Test-Compose)) { exit 1 }
        & docker @compose stop @services
        exit $LASTEXITCODE
    }
}
