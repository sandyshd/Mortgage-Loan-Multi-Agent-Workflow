<#
.SYNOPSIS
    Deploy the Mortgage Loan Origination demo to Azure.

.DESCRIPTION
    End-to-end deployment:
      1. Creates the resource group (if needed)
      2. Deploys Bicep infrastructure (AI Foundry, App Service, RBAC, etc.)
      3. Creates Foundry agents via the new Agents REST API
      4. Deploys application code to App Service via zip deploy
      5. Opens the web app in the browser

.PARAMETER ResourceGroup
    Azure resource group name. Default: rg-mortgage-demo

.PARAMETER Location
    Azure region. Default: eastus2

.PARAMETER ProjectName
    Base name for resources. Default: mortgage-demo

.EXAMPLE
    .\deploy.ps1
    .\deploy.ps1 -ResourceGroup "my-rg" -Location "westus2" -ProjectName "berkadia"
#>

param(
    [string]$ResourceGroup = "rg-mortgage-demo",
    [string]$Location = "eastus2",
    [string]$ProjectName = "mortgage-demo"
)

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "  Mortgage Demo — Full Azure Deployment" -ForegroundColor Cyan
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host ""

# ── 0. Check prerequisites ───────────────────────────────────────────
Write-Host "[0/5] Checking prerequisites ..." -ForegroundColor Yellow
$azVersion = az version 2>$null | ConvertFrom-Json
if (-not $azVersion) {
    Write-Error "Azure CLI not found. Install from https://aka.ms/installazurecli"
    exit 1
}
$account = az account show 2>$null | ConvertFrom-Json
if (-not $account) {
    Write-Error "Not logged in. Run 'az login' first."
    exit 1
}
Write-Host "  Subscription: $($account.name) ($($account.id))" -ForegroundColor Gray

# ── 1. Create resource group ─────────────────────────────────────────
Write-Host ""
Write-Host "[1/5] Creating resource group '$ResourceGroup' in '$Location' ..." -ForegroundColor Yellow
az group create --name $ResourceGroup --location $Location --output none
Write-Host "  Done." -ForegroundColor Green

# ── 2. Deploy Bicep infrastructure ───────────────────────────────────
Write-Host ""
Write-Host "[2/5] Deploying Bicep infrastructure ..." -ForegroundColor Yellow
$jsonOutput = az deployment group create `
    --resource-group $ResourceGroup `
    --template-file infra/main.bicep `
    --parameters projectName=$ProjectName `
    --query "properties.outputs" `
    --output json

if ($LASTEXITCODE -ne 0) {
    Write-Error "Bicep deployment failed."
    exit 1
}

$deployment = $jsonOutput | ConvertFrom-Json

$webAppName     = $deployment.webAppName.value
$webAppUrl      = $deployment.webAppUrl.value
$projectEndpoint = $deployment.projectEndpoint.value
$modelDeployment = $deployment.modelDeployment.value

Write-Host "  Web App:          $webAppName" -ForegroundColor Gray
Write-Host "  Web App URL:      $webAppUrl" -ForegroundColor Gray
Write-Host "  Project Endpoint: $projectEndpoint" -ForegroundColor Gray
Write-Host "  Model:            $modelDeployment" -ForegroundColor Gray
Write-Host "  RBAC:             Cognitive Services OpenAI User (auto-assigned)" -ForegroundColor Gray
Write-Host "  Done." -ForegroundColor Green

# ── 3. Create Foundry agents ─────────────────────────────────────────
Write-Host ""
Write-Host "[3/5] Creating Foundry agents ..." -ForegroundColor Yellow

# Update local .env so create_agents.py picks up the deployed endpoint
$envContent = @"
PROJECT_ENDPOINT=$projectEndpoint
MODEL_DEPLOYMENT=$modelDeployment
"@
$envContent | Set-Content -Path ".env" -Encoding UTF8

python src/foundry/create_agents.py
if ($LASTEXITCODE -ne 0) {
    Write-Error "Agent creation failed."
    exit 1
}
Write-Host "  Done." -ForegroundColor Green

# ── 4. Deploy application code to App Service ────────────────────────
Write-Host ""
Write-Host "[4/5] Deploying application code to App Service ..." -ForegroundColor Yellow

# Create a zip of the application (exclude dev/local files)
$zipPath = Join-Path $env:TEMP "mortgage-demo-deploy.zip"
if (Test-Path $zipPath) { Remove-Item $zipPath }

# Use Compress-Archive with explicit file list
$filesToInclude = @(
    "requirements.txt",
    "startup.sh",
    ".env"
)
$foldersToInclude = @(
    "src",
    "frontend",
    "data"
)

# Create the zip
$stagingDir = Join-Path $env:TEMP "mortgage-demo-staging"
if (Test-Path $stagingDir) { Remove-Item $stagingDir -Recurse -Force }
New-Item -ItemType Directory -Path $stagingDir | Out-Null

foreach ($f in $filesToInclude) {
    if (Test-Path $f) {
        Copy-Item $f -Destination $stagingDir
    }
}
foreach ($d in $foldersToInclude) {
    if (Test-Path $d) {
        Copy-Item $d -Destination (Join-Path $stagingDir $d) -Recurse
    }
}

Compress-Archive -Path "$stagingDir\*" -DestinationPath $zipPath -Force
Remove-Item $stagingDir -Recurse -Force

az webapp deploy `
    --resource-group $ResourceGroup `
    --name $webAppName `
    --src-path $zipPath `
    --type zip `
    --output none

if ($LASTEXITCODE -ne 0) {
    Write-Warning "Zip deploy failed. Trying 'az webapp up' as fallback ..."
    az webapp up `
        --resource-group $ResourceGroup `
        --name $webAppName `
        --runtime "PYTHON:3.11" `
        --output none
}

Remove-Item $zipPath -ErrorAction SilentlyContinue
Write-Host "  Done." -ForegroundColor Green

# ── 5. Open the app ──────────────────────────────────────────────────
Write-Host ""
Write-Host "[5/5] Deployment complete!" -ForegroundColor Yellow
Write-Host ""
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "  Web App URL: $webAppUrl" -ForegroundColor Green
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Opening browser ..." -ForegroundColor Gray
Start-Process $webAppUrl

Write-Host ""
Write-Host "Next steps:" -ForegroundColor Yellow
Write-Host "  1. Wait ~60 seconds for the app to warm up" -ForegroundColor Gray
Write-Host "  2. Load a sample application in the web UI" -ForegroundColor Gray
Write-Host "  3. Click 'Submit' then 'Run Workflow'" -ForegroundColor Gray
Write-Host ""
