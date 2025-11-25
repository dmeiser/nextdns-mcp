<#
.SYNOPSIS
    End-to-end validation of NextDNS MCP via Docker MCP Gateway

.DESCRIPTION
    This script performs a complete E2E test via Docker MCP:
    1. Loads configuration from .env file
    2. Builds the Docker image
    3. Imports the catalog.yaml into Docker MCP
    4. Enables the server
    5. Configures API key (via secrets or CI mode)
    6. Runs all tools via run_all_tools.ps1

.PARAMETER EnvFile
    Path to environment file (default: .env)

.EXAMPLE
    .\gateway_e2e_run.ps1

.EXAMPLE
    $env:CI = "true"; .\gateway_e2e_run.ps1

.EXAMPLE
    $env:ALLOW_LIVE_WRITES = "true"; .\gateway_e2e_run.ps1

.NOTES
    Requires Docker Desktop with MCP support
    Set CI=true for CI/CD environments (injects API key into catalog)
#>

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
        $validationProfile = (Get-Content $validationProfileFile -Raw).Trim()
        Write-Info "Validation profile created: $validationProfile"
        
        # Non-interactive cleanup for CI or when ALLOW_LIVE_WRITES is false
        $isCI = $env:CI -eq 'true'
        if ($isCI -or $allowWrites -ne 'true') {
            Write-Info "Auto-deleting validation profile (CI or non-writes mode)"
            try {
                docker mcp tools call deleteProfile "profile_id=$validationProfile" 2>&1 | Out-Null
                Write-Success "Validation profile deletion attempted"
            } catch {
                Write-Warn "Failed to delete validation profile"
            }
        } else {
            $response = Read-Host "Delete validation profile ${validationProfile}? (yes/no)"
            if ($response -eq "yes") {
                Write-Info "Deleting validation profile..."
                try {
                    docker mcp tools call deleteProfile "profile_id=$validationProfile" 2>&1 | Out-Null
                    Write-Success "Validation profile deleted"
                } catch {
                    Write-Warn "Failed to delete validation profile"
                }
            } else {
                Write-Info "Keeping validation profile for manual inspection"
            }
        }
        
        Remove-Item $validationProfileFile -ErrorAction SilentlyContinue
    }
    
    Write-Success "Cleanup complete"
}

# Register cleanup on exit
try {
    # Step 1: Build Docker image
    Write-Info ""
    Write-Info "Step 1: Building Docker image..."
    Push-Location $ProjectDir
    docker build -t nextdns-mcp:latest . 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to build Docker image"
    }
    Pop-Location
    Write-Success "Docker image built"

    # Step 2: Prepare catalog with API key (CI-specific)
    Write-Info ""
    Write-Info "Step 2: Preparing catalog..."
    
    $tempCatalog = Join-Path $ArtifactsDir "catalog-temp.yaml"
    Copy-Item (Join-Path $ProjectDir "catalog.yaml") $tempCatalog
    
    # In CI, inject API key directly into env section (bypass secrets mechanism)
    $isCI = $env:CI -eq 'true'
    if ($isCI) {
        Write-Info "CI environment detected - injecting API key into catalog env section"
        
        # Use Python to properly parse and modify YAML (same as bash script)
        $pythonScript = @"
import yaml
import sys

with open('$tempCatalog', 'r') as f:
    catalog = yaml.safe_load(f)

# Add API key to env section
if 'registry' in catalog and 'nextdns' in catalog['registry']:
    if 'env' not in catalog['registry']['nextdns']:
        catalog['registry']['nextdns']['env'] = []
    
    # Add or update NEXTDNS_API_KEY
    env_list = catalog['registry']['nextdns']['env']
    found = False
    for env_var in env_list:
        if env_var.get('name') == 'NEXTDNS_API_KEY':
            env_var['value'] = '$apiKey'
            found = True
            break
    
    if not found:
        env_list.insert(0, {
            'name': 'NEXTDNS_API_KEY',
            'value': '$apiKey',
            'description': 'NextDNS API key (injected in CI)'
        })
    
    with open('$tempCatalog', 'w') as f:
        yaml.dump(catalog, f, default_flow_style=False, sort_keys=False)
    
    sys.exit(0)
else:
    sys.exit(1)
"@
        
        $pythonScript | python3 -
        if ($LASTEXITCODE -eq 0) {
            Write-Success "API key injected into catalog"
        } else {
            Write-ErrorMsg "Failed to inject API key into catalog"
            Remove-Item $tempCatalog -ErrorAction SilentlyContinue
            exit 1
        }
    }
    
    # Import catalog
    Write-Info "Importing catalog..."
    docker mcp catalog import $tempCatalog 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-ErrorMsg "Failed to import catalog"
        Remove-Item $tempCatalog -ErrorAction SilentlyContinue
        exit 1
    }
    Write-Success "Catalog imported"
    Remove-Item $tempCatalog -ErrorAction SilentlyContinue

    # Step 3: Configure additional environment variables (if not in CI)
    Write-Info ""
    Write-Info "Step 3: Configuring additional environment variables..."
    
    if (-not $isCI) {
        # Set NEXTDNS_READABLE_PROFILES
        $readableProfiles = [Environment]::GetEnvironmentVariable('NEXTDNS_READABLE_PROFILES', 'Process')
        if ([string]::IsNullOrWhiteSpace($readableProfiles)) { $readableProfiles = 'ALL' }
        
        # Set NEXTDNS_WRITABLE_PROFILES
        $writableProfiles = [Environment]::GetEnvironmentVariable('NEXTDNS_WRITABLE_PROFILES', 'Process')
        if ([string]::IsNullOrWhiteSpace($writableProfiles)) { $writableProfiles = 'ALL' }
        
        # Create config YAML
        $configYaml = @"
nextdns:
  env:
    NEXTDNS_READABLE_PROFILES: "$readableProfiles"
    NEXTDNS_WRITABLE_PROFILES: "$writableProfiles"
    NEXTDNS_READ_ONLY: "false"
"@
        
        $tempConfig = Join-Path $ArtifactsDir "config-temp.yaml"
        Set-Content -Path $tempConfig -Value $configYaml -NoNewline
        
        # Write config
        Get-Content $tempConfig | docker mcp config write 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-ErrorMsg "Failed to configure environment variables"
            Remove-Item $tempConfig -ErrorAction SilentlyContinue
            exit 1
        }
        Write-Success "Environment variables configured"
        Remove-Item $tempConfig -ErrorAction SilentlyContinue
    } else {
        Write-Success "CI mode - all configuration in catalog"
    }

    # Step 4: Enable server (AFTER config is set)
    Write-Info ""
    Write-Info "Step 4: Enabling server..."
    docker mcp server enable nextdns 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to enable server"
    }
    Write-Success "Server enabled"

    # Debug: Show API key length (not the actual key)
    Write-Info "API key length: $($apiKey.Length) characters"

    # Check if we're in CI or if file-based secrets already exist
    $homeDir = if ($env:HOME) { $env:HOME } else { $env:USERPROFILE }
    $secretsFile = Join-Path $homeDir ".docker/mcp/secrets.env"
    if ($isCI) {
        Write-Info "CI environment detected - using file-based secrets from $secretsFile"
        Write-Success "API key configured via file-based secrets"
    } elseif (Test-Path $secretsFile) {
        Write-Info "File-based secrets detected at $secretsFile"
        Write-Success "API key configured via file-based secrets"
    } else {
        # Try to set the secret via Docker Desktop API
        try {
            Write-Output $apiKey | docker mcp secret set nextdns.api_key 2>&1 | Out-Null
            if ($LASTEXITCODE -eq 0) {
                Write-Success "API key configured via Docker Desktop"
            } else {
                throw "docker mcp secret set returned exit code $LASTEXITCODE"
            }
        } catch {
            Write-ErrorMsg "Failed to configure API key via Docker Desktop"
            Write-ErrorMsg "Error: $_"
            exit 1
        }
    }

    # Step 5: Wait for server readiness
    Write-Info ""
    Write-Info "Step 5: Waiting for server readiness..."
    
    $maxAttempts = 30
    $attempt = 0
    $ready = $false
    
    while ($attempt -lt $maxAttempts) {
        docker mcp tools ls 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) {
            $ready = $true
            Write-Success "Server is ready"
            break
        }
        
        $attempt++
        if ($attempt -ge $maxAttempts) {
            Write-ErrorMsg "Server readiness timeout"
            Write-ErrorMsg "Check logs: docker mcp logs"
            exit 1
        }
        
        Start-Sleep -Seconds 1
    }

    # Step 6: Run all tools
    Write-Info ""
    Write-Info "Step 6: Running all tools..."
    $runToolsScript = Join-Path $ScriptDir "run_all_tools.ps1"
    & $runToolsScript -AllowWrites $allowWritesBool
    
    if ($LASTEXITCODE -ne 0) {
        throw "Tool execution failed"
    }
    Write-Success "E2E test completed successfully"

} catch {
    Write-ErrorMsg "E2E test failed: $_"
    exit 1
} finally {
    Invoke-Cleanup
}

exit 0
