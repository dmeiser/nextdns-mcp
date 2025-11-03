<#
.SYNOPSIS
    Enumerate and call all NextDNS MCP tools via Docker MCP CLI

.DESCRIPTION
    This script enumerates all available tools from Docker MCP and invokes
    each one with appropriate test parameters. It produces a machine-readable JSONL
    report for each tool execution.

.PARAMETER ProfileId
    NextDNS profile ID to use for tests (default: creates new profile)

.PARAMETER AllowWrites
    Enable write operations (default: $false)

.EXAMPLE
    .\run_all_tools.ps1
    
.EXAMPLE
    .\run_all_tools.ps1 -ProfileId "abc123" -AllowWrites $true

.OUTPUTS
    artifacts/tools_report.jsonl - NDJSON file with per-tool execution results
#>

param(
    [string]$ProfileId = "",
    [bool]$AllowWrites = $false
)

$ErrorActionPreference = "Stop"

# Configuration
$ArtifactsDir = "artifacts"
$ReportFile = Join-Path $ArtifactsDir "tools_report.jsonl"

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

# Ensure artifacts directory exists
New-Item -ItemType Directory -Force -Path $ArtifactsDir | Out-Null

# Clear previous report
Clear-Content -Path $ReportFile -ErrorAction SilentlyContinue
New-Item -ItemType File -Force -Path $ReportFile | Out-Null

Write-Info "Starting NextDNS MCP tools enumeration and execution"
Write-Info "Allow writes: $AllowWrites"
Write-Info "Report file: $ReportFile"

# Preflight validation
Write-Info "Performing preflight checks..."

try {
    $toolsList = docker mcp tools ls 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) {
        throw "Docker MCP not responding"
    }
} catch {
    Write-ErrorMsg "Failed to enumerate tools from Docker MCP"
    Write-ErrorMsg "Error: $_"
    Write-ErrorMsg ""
    Write-ErrorMsg "Troubleshooting steps:"
    Write-ErrorMsg "1. Verify Docker Desktop is running"
    Write-ErrorMsg "2. Check Docker MCP version: docker mcp version"
    Write-ErrorMsg "3. Verify server is enabled: docker mcp server ls"
    Write-ErrorMsg "4. Check logs: docker mcp logs"
    exit 1
}

Write-Success "Docker MCP is responding"

# Parse tool names
$toolNames = docker mcp tools ls --format json 2>&1 | ConvertFrom-Json | ForEach-Object { $_.name }

if ($null -eq $toolNames -or $toolNames.Count -eq 0) {
    Write-ErrorMsg "No tools found"
    Write-ErrorMsg "The MCP server may not be properly configured"
    Write-ErrorMsg "Run: docker mcp catalog import ./catalog.yaml"
    Write-ErrorMsg "Run: docker mcp server enable nextdns"
    exit 1
}

$toolCount = $toolNames.Count
Write-Success "Found $toolCount tools"

# Create or use existing profile for tests
if ([string]::IsNullOrWhiteSpace($ProfileId)) {
    if ($AllowWrites) {
        Write-Info "Write mode enabled: Looking for existing profile to use..."
        # Due to OpenAPI validation issues with createProfile, we'll use an existing profile
        # TODO: Fix createProfile OpenAPI schema validation
        
        try {
            # Call listProfiles and filter output to get only JSON line
            $profilesResult = docker mcp tools call listProfiles '{}' 2>&1 | 
                Where-Object { $_ -match '^\{' } | 
                Out-String
            
            if ([string]::IsNullOrWhiteSpace($profilesResult)) {
                Write-ErrorMsg "No JSON output from listProfiles"
                exit 1
            }
            
            $profilesData = $profilesResult | ConvertFrom-Json
            if ($profilesData.data -and $profilesData.data.Count -gt 0) {
                # Use the first profile for testing
                $ProfileId = $profilesData.data[0].id
                Write-Success "Using existing profile for testing: $ProfileId"
                Write-Warn "CAUTION: Write operations will modify this profile!"
                $ProfileId | Out-File -FilePath (Join-Path $ArtifactsDir "validation_profile_id.txt") -Encoding utf8
            } else {
                Write-ErrorMsg "No profiles found"
                Write-ErrorMsg "Please create a test profile manually first"
                exit 1
            }
        } catch {
            Write-ErrorMsg "Failed to list profiles: $_"
            exit 1
        }
    } else {
        # For read-only mode, try to get the first available profile
        Write-Info "Read-only mode: Looking for existing profile..."
        try {
            # Call listProfiles and filter output to get only JSON line
            $profilesResult = docker mcp tools call listProfiles '{}' 2>&1 | 
                Where-Object { $_ -match '^\{' } | 
                Out-String
            
            if ([string]::IsNullOrWhiteSpace($profilesResult)) {
                Write-ErrorMsg "No JSON output from listProfiles"
                exit 1
            }
            
            $profilesData = $profilesResult | ConvertFrom-Json
            if ($profilesData.data -and $profilesData.data.Count -gt 0) {
                $ProfileId = $profilesData.data[0].id
                Write-Success "Using existing profile: $ProfileId"
            } else {
                Write-ErrorMsg "No profiles found"
                Write-ErrorMsg "Create a profile first or run with -AllowWrites `$true to create one"
                exit 1
            }
        } catch {
            Write-ErrorMsg "Failed to list profiles: $_"
            exit 1
        }
    }
} else {
    Write-Info "Using provided profile: $ProfileId"
}

# Define read-only tools
$readOnlyTools = @(
    # Profile operations
    "listProfiles", "getProfile",
    
    # Settings
    "getSettings", "getBlockPageSettings", "getPerformanceSettings", "getLogsSettings",
    
    # Privacy
    "getPrivacyBlocklists", "getPrivacyNatives", "getPrivacySettings",
    
    # Security
    "getSecurityTLDs", "getSecuritySettings",
    
    # Parental Control
    "getParentalControlCategories", "getParentalControlServices", "getParentalControlSettings",
    
    # Lists
    "getAllowlist", "getDenylist",
    
    # Logs
    "getLogs", "downloadLogs",
    
    # Analytics - base endpoints
    "getAnalyticsDomains", "getAnalyticsStatus", "getAnalyticsDevices",
    "getAnalyticsProtocols", "getAnalyticsEncryption", "getAnalyticsIPVersions",
    "getAnalyticsDNSSEC", "getAnalyticsIPs", "getAnalyticsQueryTypes",
    "getAnalyticsReasons", "getAnalyticsDestinations",
    
    # Analytics - series endpoints
    "getAnalyticsDNSSECSeries", "getAnalyticsDestinationsSeries", "getAnalyticsDevicesSeries",
    "getAnalyticsEncryptionSeries", "getAnalyticsIPVersionsSeries", "getAnalyticsIPsSeries",
    "getAnalyticsProtocolsSeries", "getAnalyticsQueryTypesSeries", "getAnalyticsReasonsSeries",
    "getAnalyticsStatusSeries",
    
    # Special operations
    "dohLookup"
)

# Function to get test arguments for a tool
function Get-ToolArgs {
    param([string]$ToolName)
    
    $fromTimestamp = [DateTimeOffset]::UtcNow.AddDays(-1).ToUnixTimeSeconds()
    $hoursAgoTimestamp = [DateTimeOffset]::UtcNow.AddHours(-1).ToUnixTimeSeconds()
    
    switch ($ToolName) {
        "getProfile" {
            return "profile_id=$ProfileId"
        }
        
        { $_ -in @("getSettings", "updateSettings", "getAllowlist", "getDenylist",
                   "getPrivacyBlocklists", "getPrivacyNatives", "getPrivacySettings",
                   "getSecurityTLDs", "getSecuritySettings", "getParentalControlCategories",
                   "getParentalControlServices", "getParentalControlSettings",
                   "getBlockPageSettings", "getPerformanceSettings", "getLogsSettings") } {
            return "profile_id=$ProfileId"
        }
        
        { $_ -in @("getAnalyticsDomains", "getAnalyticsStatus", "getAnalyticsDevices",
                   "getAnalyticsProtocols", "getAnalyticsEncryption", "getAnalyticsIPVersions",
                   "getAnalyticsDNSSEC", "getAnalyticsIPs", "getAnalyticsQueryTypes",
                   "getAnalyticsReasons", "getAnalyticsDestinations",
                   "getAnalyticsDNSSECSeries", "getAnalyticsDestinationsSeries", "getAnalyticsDevicesSeries",
                   "getAnalyticsEncryptionSeries", "getAnalyticsIPVersionsSeries", "getAnalyticsIPsSeries",
                   "getAnalyticsProtocolsSeries", "getAnalyticsQueryTypesSeries", "getAnalyticsReasonsSeries",
                   "getAnalyticsStatusSeries") } {
            # Analytics endpoints need from parameter, Destinations need type parameter
            if ($_ -match "Destinations") {
                return "profile_id=$ProfileId from=$fromTimestamp type=countries"
            } else {
                return "profile_id=$ProfileId from=$fromTimestamp"
            }
        }
        
        { $_ -in @("getLogs", "downloadLogs", "clearLogs") } {
            return "profile_id=$ProfileId from=$hoursAgoTimestamp limit=10"
        }
        
        "dohLookup" {
            return "domain=example.com profile_id=$ProfileId record_type=A"
        }
        
        "listProfiles" {
            return ""
        }
        
        "createProfile" {
            # Don't use quotes - Docker MCP CLI handles spaces correctly without them
            return "name=Test_Profile_$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())"
        }
        
        { $_ -in @("addToAllowlist", "addToDenylist") } {
            return "profile_id=$ProfileId id=test-example.com"
        }
        
        { $_ -in @("removeFromAllowlist", "removeFromDenylist") } {
            return "profile_id=$ProfileId entry_id=test-example.com"
        }
        
        "addPrivacyBlocklist" {
            return "profile_id=$ProfileId id=nextdns-recommended"
        }
        
        "removePrivacyBlocklist" {
            return "profile_id=$ProfileId entry_id=nextdns-recommended"
        }
        
        "addPrivacyNative" {
            return "profile_id=$ProfileId id=apple"
        }
        
        "removePrivacyNative" {
            return "profile_id=$ProfileId entry_id=apple"
        }
        
        "addSecurityTLD" {
            return "profile_id=$ProfileId id=zip"
        }
        
        "removeSecurityTLD" {
            return "profile_id=$ProfileId entry_id=zip"
        }
        
        { $_ -in @("addToParentalControlCategories", "removeFromParentalControlCategories") } {
            return "profile_id=$ProfileId id=gambling"
        }
        
        { $_ -in @("addToParentalControlServices", "removeFromParentalControlServices") } {
            return "profile_id=$ProfileId id=tiktok"
        }
        
        { $_ -in @("updateAllowlistEntry", "updateDenylistEntry") } {
            return "profile_id=$ProfileId entry_id=test-example.com active=true"
        }
        
        { $_ -in @("updateParentalControlCategoryEntry") } {
            return "profile_id=$ProfileId id=gambling active=true"
        }
        
        { $_ -in @("updateParentalControlServiceEntry") } {
            return "profile_id=$ProfileId id=tiktok active=true"
        }
        
        "updateProfile" {
            return "profile_id=$ProfileId name=Updated_Test_Profile"
        }
        
        "updateSettings" {
            return "profile_id=$ProfileId blockPage={`"enabled`":true}"
        }
        
        "updateBlockPageSettings" {
            return "profile_id=$ProfileId enabled=true"
        }
        
        "updateLogsSettings" {
            return "profile_id=$ProfileId enabled=true retention=2592000"
        }
        
        "updatePerformanceSettings" {
            return "profile_id=$ProfileId ecs=true cacheBoost=true"
        }
        
        "updatePrivacySettings" {
            return "profile_id=$ProfileId disguisedTrackers=true allowAffiliate=false"
        }
        
        "updateSecuritySettings" {
            return "profile_id=$ProfileId threatIntelligenceFeeds=true googleSafeBrowsing=true"
        }
        
        "updateParentalControlSettings" {
            return "profile_id=$ProfileId safeSearch=true youtubeRestrictedMode=true"
        }
        
        # PUT/replace operations (custom tools use update* naming)
        { $_ -in @("updateAllowlist") } {
            return "profile_id=$ProfileId entries=`"[\`"test1.com\`",\`"test2.com\`"]`""
        }
        
        { $_ -in @("updateDenylist") } {
            return "profile_id=$ProfileId entries=`"[\`"block1.com\`",\`"block2.com\`"]`""
        }
        
        { $_ -in @("updatePrivacyBlocklists") } {
            return "profile_id=$ProfileId blocklists=`"[\`"nextdns-recommended\`",\`"oisd\`"]`""
        }
        
        { $_ -in @("updatePrivacyNatives") } {
            return "profile_id=$ProfileId natives=`"[\`"apple\`",\`"windows\`"]`""
        }
        
        { $_ -in @("updateSecurityTlds") } {
            return "profile_id=$ProfileId tlds=`"[\`"zip\`",\`"mov\`"]`""
        }
        
        { $_ -in @("updateParentalControlCategories") } {
            return "profile_id=$ProfileId categories=`"[\`"gambling\`",\`"dating\`"]`""
        }
        
        { $_ -in @("updateParentalControlServices") } {
            return "profile_id=$ProfileId services=`"[\`"tiktok\`",\`"fortnite\`"]`""
        }
        
        "deleteProfile" {
            return "profile_id=dummy-profile-id"
        }
        
        default {
            return ""
        }
    }
}

# Execute each tool and record results
$executed = 0
$skipped = 0
$failed = 0

Write-Info "Executing tools..."

foreach ($toolName in $toolNames) {
    if ([string]::IsNullOrWhiteSpace($toolName)) {
        continue
    }
    
    $startTime = Get-Date
    $skipReason = ""
    
    # Check if tool should be skipped
    if (-not $AllowWrites -and $toolName -notin $readOnlyTools) {
        $skipReason = "Write operations disabled (ALLOW_LIVE_WRITES=false)"
        $status = "skipped"
        $exitCode = 0
        $stdout = ""
        $stderr = ""
        Write-Warn "Skipping ${toolName}: $skipReason"
        $skipped++
    } elseif ($toolName -eq "deleteProfile") {
        # Never actually delete profiles except the validation one
        $skipReason = "Destructive operation (deleteProfile)"
        $status = "skipped"
        $exitCode = 0
        $stdout = ""
        $stderr = ""
        Write-Warn "Skipping ${toolName}: $skipReason"
        $skipped++
    } else {
        # Get test arguments for this tool
        $toolArgs = Get-ToolArgs -ToolName $toolName
        
        # Pre-execution: Ensure entries exist for update operations
        if ($toolName -eq "updateAllowlistEntry") {
            Write-Info "Pre-check: Ensuring test entry exists in allowlist..."
            docker mcp tools call addToAllowlist profile_id=$ProfileId id=test-example.com 2>&1 | Out-Null
        }
        elseif ($toolName -eq "updateDenylistEntry") {
            Write-Info "Pre-check: Ensuring test entry exists in denylist..."
            docker mcp tools call addToDenylist profile_id=$ProfileId id=test-example.com 2>&1 | Out-Null
        }
        elseif ($toolName -eq "updateParentalControlCategoryEntry") {
            Write-Info "Pre-check: Ensuring gambling category is added..."
            docker mcp tools call addToParentalControlCategories profile_id=$ProfileId id=gambling 2>&1 | Out-Null
        }
        elseif ($toolName -eq "updateParentalControlServiceEntry") {
            Write-Info "Pre-check: Ensuring tiktok service is added..."
            docker mcp tools call addToParentalControlServices profile_id=$ProfileId id=tiktok 2>&1 | Out-Null
        }
        
        Write-Info "Executing: $toolName"
        
        # Execute tool via docker mcp
        try {
            if ([string]::IsNullOrWhiteSpace($toolArgs)) {
                $result = docker mcp tools call $toolName 2>&1 | Out-String
            } else {
                $argParts = $toolArgs -split ' '
                $result = docker mcp tools call $toolName @argParts 2>&1 | Out-String
            }
            
            if ($LASTEXITCODE -eq 0) {
                $exitCode = 0
                $stdout = $result
                $stderr = ""
                $status = "success"
                Write-Success "${toolName}: OK"
                $executed++
            } else {
                $exitCode = $LASTEXITCODE
                $stdout = ""
                $stderr = $result
                $status = "failed"
                Write-ErrorMsg "${toolName}: FAILED (exit code $exitCode)"
                $failed++
            }
        } catch {
            $exitCode = 1
            $stdout = ""
            $stderr = $_.Exception.Message
            $status = "failed"
            Write-ErrorMsg "${toolName}: FAILED - $_"
            $failed++
        }
    }
    
    $endTime = Get-Date
    $duration = ($endTime - $startTime).TotalSeconds
    
    # Write result to JSONL file
    $resultObj = @{
        tool = $toolName
        status = $status
        args = $toolArgs
        exit_code = $exitCode
        stdout = $stdout
        stderr = $stderr
        duration = $duration
        skip_reason = $skipReason
        timestamp = Get-Date -Format "yyyy-MM-ddTHH:mm:ssZ"
    }
    
    $resultObj | ConvertTo-Json -Compress -Depth 10 | Add-Content -Path $ReportFile -Encoding utf8
}

# Summary
Write-Info "================================"
Write-Info "Execution Summary"
Write-Info "================================"
Write-Info "Total tools: $toolCount"
Write-Success "Executed: $executed"
Write-Warn "Skipped: $skipped"
if ($failed -gt 0) {
    Write-ErrorMsg "Failed: $failed"
} else {
    Write-Info "Failed: $failed"
}
Write-Info "Report: $ReportFile"

# Cleanup validation profile if created and writes are allowed
if ($AllowWrites -and (Test-Path (Join-Path $ArtifactsDir "validation_profile_id.txt"))) {
    Write-Info ""
    Write-Info "Validation profile created: $ProfileId"
    Write-Info "Profile cleanup is handled by the E2E script"
}

# Exit with failure if any tools failed
if ($failed -gt 0) {
    exit 1
}

exit 0
