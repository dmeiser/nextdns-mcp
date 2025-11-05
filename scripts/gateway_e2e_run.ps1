<#
.SYNOPSIS
    End-to-end validation of NextDNS MCP via Docker MCP Gateway

.DESCRIPTION
    This script performs a complete E2E test via Docker MCP:
    1. Loads configuration from .env file
    2. Builds the Docker image
    3. Imports the catalog.yaml into Docker MCP
    4. Enables the server
    5. Configures secrets and settings
    6. Runs all tools via run_all_tools.ps1
    7. Optionally cleans up

.PARAMETER EnvFile
    Path to environment file (default: .env, fallback: .env.example)

.PARAMETER Cleanup
    Whether to cleanup (disable server) after test (default: $false)

.EXAMPLE
    .\gateway_e2e_run.ps1

.EXAMPLE
    .\gateway_e2e_run.ps1 -EnvFile .env.test -Cleanup $true

.NOTES
    Requires Docker Desktop with MCP support
#>

param(
    [string]$EnvFile = "",
    [bool]$Cleanup = $false
)

$ErrorActionPreference = "Stop"

# Logging functions
function Write-Info {
    param([string]$Message)
    Write-Host "[INFO] $Message" -ForegroundColor Blue
}

function Write-Success {
    param([string]$Message)
    Write-Host "[SUCCESS] $Message" -ForegroundColor Green
}

function Write-Warn {
    param([string]$Message)
    Write-Host "[WARN] $Message" -ForegroundColor Yellow
}

function Write-ErrorMsg {
    param([string]$Message)
    Write-Host "[ERROR] $Message" -ForegroundColor Red
}

# Configuration
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
$ArtifactsDir = Join-Path $ProjectDir "artifacts"

# Load environment file
if ([string]::IsNullOrWhiteSpace($EnvFile)) {
    $defaultEnv = Join-Path $ProjectDir ".env"
    $exampleEnv = Join-Path $ProjectDir ".env.example"
    
    if (Test-Path $defaultEnv) {
        $EnvFile = $defaultEnv
        Write-Info "Using default .env file"
    } elseif (Test-Path $exampleEnv) {
        $EnvFile = $exampleEnv
        Write-Warn "Using .env.example as fallback"
    } else {
        Write-ErrorMsg "No environment file found"
        Write-ErrorMsg "Please create .env or provide env file path with -EnvFile parameter"
        exit 1
    }
}

if (-not (Test-Path $EnvFile)) {
    Write-ErrorMsg "Environment file not found: $EnvFile"
    exit 1
}

Write-Info "Loading environment from: $EnvFile"

# Load environment variables from file
Get-Content $EnvFile | ForEach-Object {
    if ($_ -match '^\s*([^#][^=]*)\s*=\s*(.*)$') {
        $name = $matches[1].Trim()
        $value = $matches[2].Trim()
        # Remove quotes if present
        $value = $value -replace '^["'']|["'']$', ''
        [Environment]::SetEnvironmentVariable($name, $value, 'Process')
    }
}

# Validate required variables
$apiKey = [Environment]::GetEnvironmentVariable('NEXTDNS_API_KEY', 'Process')
if ([string]::IsNullOrWhiteSpace($apiKey) -or $apiKey -eq 'your-api-key-here') {
    Write-ErrorMsg "NEXTDNS_API_KEY is not set or is the default placeholder"
    Write-ErrorMsg "Please set your NextDNS API key in $EnvFile"
    exit 1
}

# Set defaults
$allowWrites = [Environment]::GetEnvironmentVariable('ALLOW_LIVE_WRITES', 'Process')
if ([string]::IsNullOrWhiteSpace($allowWrites)) { $allowWrites = 'false' }
$allowWritesBool = $allowWrites -eq 'true'

Write-Info "================================"
Write-Info "NextDNS MCP Gateway E2E Test"
Write-Info "================================"
Write-Info "Allow writes: $allowWrites"
Write-Info "Artifacts: $ArtifactsDir"

# Ensure artifacts directory exists
New-Item -ItemType Directory -Force -Path $ArtifactsDir | Out-Null

# Cleanup function
function Invoke-Cleanup {
    Write-Info ""
    
    # Cleanup validation profile if created
    $validationProfileFile = Join-Path $ArtifactsDir "validation_profile_id.txt"
    if (Test-Path $validationProfileFile) {
        $validationProfile = Get-Content $validationProfileFile -Raw
        $validationProfile = $validationProfile.Trim()
        Write-Info "Validation profile created: $validationProfile"
        
        if ($allowWritesBool) {
            $response = Read-Host "Delete validation profile ${validationProfile}? (yes/no)"
            if ($response -eq "yes") {
                Write-Info "Deleting validation profile..."
                try {
                    docker mcp tools call deleteProfile profile_id=$validationProfile 2>&1 | Out-Null
                    Write-Success "Validation profile deleted"
                } catch {
                    Write-Warn "Failed to delete validation profile: $_"
                }
            } else {
                Write-Info "Keeping validation profile for manual inspection"
            }
        }
        
        Remove-Item $validationProfileFile -ErrorAction SilentlyContinue
    }
    
    if ($Cleanup) {
        Write-Info "Cleanup: Disabling server..."
        try {
            docker mcp server disable nextdns 2>&1 | Out-Null
            Write-Success "Server disabled"
        } catch {
            Write-Warn "Failed to disable server: $_"
        }
    }
    
    Write-Success "Cleanup complete"
}

# Register cleanup on exit
try {
    # Step 1: Build Docker image
    Write-Info ""
    Write-Info "Step 1: Building Docker image..."
    Push-Location $ProjectDir
    docker build -t nextdns-mcp:latest .
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to build Docker image"
    }
    Pop-Location
    Write-Success "Docker image built"

    # Step 2: Import catalog
    Write-Info ""
    Write-Info "Step 2: Importing catalog..."
    $catalogPath = Join-Path $ProjectDir "catalog.yaml"
    docker mcp catalog import $catalogPath
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to import catalog"
    }
    Write-Success "Catalog imported"

    # Step 3: Enable server
    Write-Info ""
    Write-Info "Step 3: Enabling server..."
    docker mcp server enable nextdns
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to enable server"
    }
    Write-Success "Server enabled"

    # Step 4: Configure secret
    Write-Info ""
    Write-Info "Step 4: Configuring API key secret..."
    Write-Output $apiKey | docker mcp secret set nextdns.api_key
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to set API key secret"
    }
    Write-Success "API key configured"

    # Step 5: Wait a moment for server to be ready
    Write-Info ""
    Write-Info "Step 5: Waiting for server readiness..."
    Start-Sleep -Seconds 2
    
    # Verify tools are available
    $tools = docker mcp tools ls 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to list tools: $tools"
    }
    Write-Success "Server is ready"

    # Step 6: Run all tools
    Write-Info ""
    Write-Info "Step 6: Running all tools..."
    $runToolsScript = Join-Path $ScriptDir "run_all_tools.ps1"
    & $runToolsScript -AllowWrites $allowWritesBool
    
    if ($LASTEXITCODE -ne 0) {
        throw "Tool execution failed"
    }
    Write-Success "All tools executed"

    # Summary
    Write-Info ""
    Write-Info "================================"
    Write-Info "E2E Test Complete"
    Write-Info "================================"
    $reportFile = Join-Path $ArtifactsDir "tools_report.jsonl"
    Write-Info "Report: $reportFile"
    Write-Info ""
    Write-Info "To view the report:"
    Write-Info "  Get-Content $reportFile | ForEach-Object { \$_ | ConvertFrom-Json }"
    Write-Info ""
    Write-Info "To filter by status:"
    Write-Info "  Get-Content $reportFile | ForEach-Object { \$_ | ConvertFrom-Json } | Where-Object { \$_.status -eq 'failed' }"
    Write-Info "  Get-Content $reportFile | ForEach-Object { \$_ | ConvertFrom-Json } | Where-Object { \$_.status -eq 'skipped' }"

} catch {
    Write-ErrorMsg "E2E test failed: $_"
    exit 1
} finally {
    Invoke-Cleanup
}

exit 0


param(
    [string]$EnvFile = ""
)

$ErrorActionPreference = "Stop"

# Logging functions
function Write-Info {
    param([string]$Message)
    Write-Host "[INFO] $Message" -ForegroundColor Blue
}

function Write-Success {
    param([string]$Message)
    Write-Host "[SUCCESS] $Message" -ForegroundColor Green
}

function Write-Warn {
    param([string]$Message)
    Write-Host "[WARN] $Message" -ForegroundColor Yellow
}

function Write-ErrorMsg {
    param([string]$Message)
    Write-Host "[ERROR] $Message" -ForegroundColor Red
}

# Configuration
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
$ArtifactsDir = Join-Path $ProjectDir "artifacts"

# Load environment file
if ([string]::IsNullOrWhiteSpace($EnvFile)) {
    $defaultEnv = Join-Path $ProjectDir ".env"
    $exampleEnv = Join-Path $ProjectDir ".env.example"
    
    if (Test-Path $defaultEnv) {
        $EnvFile = $defaultEnv
        Write-Info "Using default .env file"
    } elseif (Test-Path $exampleEnv) {
        $EnvFile = $exampleEnv
        Write-Warn "Using .env.example as fallback"
    } else {
        Write-ErrorMsg "No environment file found"
        Write-ErrorMsg "Please create .env or provide env file path with -EnvFile parameter"
        exit 1
    }
}

if (-not (Test-Path $EnvFile)) {
    Write-ErrorMsg "Environment file not found: $EnvFile"
    exit 1
}

Write-Info "Loading environment from: $EnvFile"

# Load environment variables from file
Get-Content $EnvFile | ForEach-Object {
    if ($_ -match '^\s*([^#][^=]*)\s*=\s*(.*)$') {
        $name = $matches[1].Trim()
        $value = $matches[2].Trim()
        # Remove quotes if present
        $value = $value -replace '^["'']|["'']$', ''
        [Environment]::SetEnvironmentVariable($name, $value, 'Process')
    }
}

# Validate required variables
$apiKey = [Environment]::GetEnvironmentVariable('NEXTDNS_API_KEY', 'Process')
if ([string]::IsNullOrWhiteSpace($apiKey) -or $apiKey -eq 'your-api-key-here') {
    Write-ErrorMsg "NEXTDNS_API_KEY is not set or is the default placeholder"
    Write-ErrorMsg "Please set your NextDNS API key in $EnvFile"
    exit 1
}

# Set defaults
$port = [Environment]::GetEnvironmentVariable('DOCKER_MCP_GATEWAY_PORT', 'Process')
if ([string]::IsNullOrWhiteSpace($port)) { $port = '3000' }

$containerName = [Environment]::GetEnvironmentVariable('DOCKER_MCP_GATEWAY_CONTAINER', 'Process')
if ([string]::IsNullOrWhiteSpace($containerName)) { $containerName = 'nextdns-mcp-gateway' }

$allowWrites = [Environment]::GetEnvironmentVariable('ALLOW_LIVE_WRITES', 'Process')
if ([string]::IsNullOrWhiteSpace($allowWrites)) { $allowWrites = 'false' }
$allowWritesBool = $allowWrites -eq 'true'

$readinessTimeout = [Environment]::GetEnvironmentVariable('GATEWAY_READINESS_TIMEOUT', 'Process')
if ([string]::IsNullOrWhiteSpace($readinessTimeout)) { $readinessTimeout = '60' }

$readinessInterval = [Environment]::GetEnvironmentVariable('GATEWAY_READINESS_INTERVAL', 'Process')
if ([string]::IsNullOrWhiteSpace($readinessInterval)) { $readinessInterval = '2' }

Write-Info "================================"
Write-Info "NextDNS MCP Gateway E2E Test"
Write-Info "================================"
Write-Info "Container: $containerName"
Write-Info "Port: $port"
Write-Info "Allow writes: $allowWrites"
Write-Info "Artifacts: $ArtifactsDir"

# Ensure artifacts directory exists
New-Item -ItemType Directory -Force -Path $ArtifactsDir | Out-Null

# Cleanup function
function Invoke-Cleanup {
    Write-Info ""
    
    # Cleanup validation profile if created (BEFORE stopping container)
    $validationProfileFile = Join-Path $ArtifactsDir "validation_profile_id.txt"
    if (Test-Path $validationProfileFile) {
        $validationProfile = Get-Content $validationProfileFile -Raw
        $validationProfile = $validationProfile.Trim()
        Write-Info "Validation profile created: $validationProfile"
        
        if ($allowWritesBool) {
            $response = Read-Host "Delete validation profile ${validationProfile}? (yes/no)"
            if ($response -eq "yes") {
                Write-Info "Deleting validation profile..."
                docker exec $containerName mcp tools call deleteProfile --args "{`"profile_id`":`"$validationProfile`"}" 2>&1 | Out-Null
                Write-Success "Validation profile deleted"
            } else {
                Write-Info "Keeping validation profile for manual inspection"
            }
        }
        
        Remove-Item $validationProfileFile -ErrorAction SilentlyContinue
    }
    
    Write-Info "Cleanup: Stopping container $containerName..."
    docker stop $containerName 2>&1 | Out-Null
    docker rm $containerName 2>&1 | Out-Null
    
    Write-Success "Cleanup complete"
}

# Register cleanup on exit
try {
    # Step 1: Build Docker image
    Write-Info ""
    Write-Info "Step 1: Building Docker image..."
    Push-Location $ProjectDir
    docker build -t nextdns-mcp:latest .
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to build Docker image"
    }
    Pop-Location
    Write-Success "Docker image built"

    # Step 2: Check if container is already running
    Write-Info ""
    Write-Info "Step 2: Checking for existing container..."
    $existingContainer = docker ps -a --format "{{.Names}}" | Where-Object { $_ -eq $containerName }
    if ($existingContainer) {
        Write-Warn "Container $containerName already exists, removing..."
        docker stop $containerName 2>&1 | Out-Null
        docker rm $containerName 2>&1 | Out-Null
    }

    # Step 3: Start the Docker MCP Gateway container
    Write-Info ""
    Write-Info "Step 3: Starting Docker MCP Gateway container..."

    # Create gateway config
    $gatewayConfig = Join-Path $ArtifactsDir "gateway-config.yaml"
    @"
servers:
  nextdns:
    env:
      NEXTDNS_API_KEY: "$apiKey"
      NEXTDNS_DEFAULT_PROFILE: ""
      NEXTDNS_HTTP_TIMEOUT: "45"
      NEXTDNS_READABLE_PROFILES: "ALL"
      NEXTDNS_WRITABLE_PROFILES: "ALL"
      NEXTDNS_READ_ONLY: "false"
"@ | Out-File -FilePath $gatewayConfig -Encoding utf8

    docker run -d `
        --name $containerName `
        -p "${port}:3000" `
        -v "${gatewayConfig}:/app/config.yaml:ro" `
        modelcontextprotocol/gateway:latest

    if ($LASTEXITCODE -ne 0) {
        throw "Failed to start Docker MCP Gateway container"
    }
    Write-Success "Container started"

    # Step 4: Import catalog
    Write-Info ""
    Write-Info "Step 4: Importing catalog..."
    $catalogPath = Join-Path $ProjectDir "catalog.yaml"
    
    # Copy catalog into container
    docker cp $catalogPath "${containerName}:/tmp/catalog.yaml"
    if ($LASTEXITCODE -ne 0) {
        Write-ErrorMsg "Failed to copy catalog to container"
        throw "Catalog copy failed"
    }
    
    # Import the catalog
    docker exec $containerName mcp import /tmp/catalog.yaml
    if ($LASTEXITCODE -ne 0) {
        Write-ErrorMsg "Failed to import catalog"
        docker logs $containerName
        throw "Catalog import failed"
    }
    Write-Success "Catalog imported"

    # Step 5: Wait for gateway readiness
    Write-Info ""
    Write-Info "Step 5: Waiting for gateway readiness (timeout: ${readinessTimeout}s)..."
    $elapsed = 0
    $ready = $false
    
    while ($elapsed -lt [int]$readinessTimeout) {
        try {
            docker exec $containerName mcp tools list 2>&1 | Out-Null
            if ($LASTEXITCODE -eq 0) {
                $ready = $true
                Write-Success "Gateway is ready"
                break
            }
        } catch {
            # Continue waiting
        }
        
        Start-Sleep -Seconds ([int]$readinessInterval)
        $elapsed += [int]$readinessInterval
        Write-Info "Waiting... (${elapsed}s elapsed)"
    }

    if (-not $ready) {
        Write-ErrorMsg "Gateway readiness timeout"
        docker logs $containerName
        throw "Gateway not ready"
    }

    # Step 6: Run all tools
    Write-Info ""
    Write-Info "Step 6: Running all tools..."
    $runToolsScript = Join-Path $ScriptDir "run_all_tools.ps1"
    & $runToolsScript -ContainerName $containerName -ProfileId "" -AllowWrites $allowWritesBool
    
    if ($LASTEXITCODE -ne 0) {
        throw "Tool execution failed"
    }
    Write-Success "All tools executed"

    # Summary
    Write-Info ""
    Write-Info "================================"
    Write-Info "E2E Test Complete"
    Write-Info "================================"
    $reportFile = Join-Path $ArtifactsDir "tools_report.jsonl"
    Write-Info "Report: $reportFile"
    Write-Info ""
    Write-Info "To view the report:"
    Write-Info "  Get-Content $reportFile | ForEach-Object { \$_ | ConvertFrom-Json }"
    Write-Info ""
    Write-Info "To filter by status:"
    Write-Info "  Get-Content $reportFile | ForEach-Object { \$_ | ConvertFrom-Json } | Where-Object { \$_.status -eq 'failed' }"
    Write-Info "  Get-Content $reportFile | ForEach-Object { \$_ | ConvertFrom-Json } | Where-Object { \$_.status -eq 'skipped' }"

} catch {
    Write-ErrorMsg "E2E test failed: $_"
    exit 1
} finally {
    Invoke-Cleanup
}

exit 0
