"""PowerShell scripts dispatched to target servers by collector v2.

Single source of truth for the 5 PowerShell payloads sent over WinRM:
``PS_COLLECT_SCRIPT``, ``PS_CHECK_UPDATES``, ``PS_COLLECT_LOGS``,
``PS_HARDWARE_SCRIPT``, ``PS_COLLECT_FAILED_LOGINS``.

Post-v1-retirement: v1 used to carry copies of these scripts inside
``collector.py`` and ``failed_logins.py``; the parity test ensured
byte-identity. With v1 gone, this module is the sole owner. Other
modules that need to dispatch one of these scripts (e.g.
``failed_logins.py``) MUST import the constant from here rather than
copy-paste — duplication would silently re-introduce the drift the
parity test used to guard.

Each script is named ``PS_<PURPOSE>`` and is invoked by the
corresponding function in ``checks.py`` (or, for the periodic
failed-login job, by ``failed_logins.py``). The PowerShell process
runs on the target server under the SYSTEM account (via the WSMan
ShellRunAs identity) and the result is read via stdout as JSON
(``Compress`` + ``Depth 3-4``).
"""

from __future__ import annotations

PS_COLLECT_SCRIPT = r"""
$cpu = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
$os = Get-CimInstance Win32_OperatingSystem
$ram = if ($os.TotalVisibleMemorySize -gt 0) { [math]::Round(($os.TotalVisibleMemorySize - $os.FreePhysicalMemory) / $os.TotalVisibleMemorySize * 100, 1) } else { -1 }
# DriveType=3 restricts to FIXED local disks. Without it, an optical drive
# (DVD/CD, DriveType=5) or removable media (USB, DriveType=2) mounted at C:/D:
# reports FreeSpace=0 on a read-only disc -> 100% "used" -> a permanent false
# critical for a drive that isn't real server storage (several fleet hosts ship
# a DVD drive at D:). A missing/optical/removable drive yields -1,
# which the detection layer skips (None-equivalent) so it never affects status.
$diskC = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='C:' AND DriveType=3"
$diskCPct = if ($diskC -and $diskC.Size -gt 0) { [math]::Round(($diskC.Size - $diskC.FreeSpace) / $diskC.Size * 100, 1) } else { -1 }
$diskD = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='D:' AND DriveType=3"
$diskDPct = if ($diskD -and $diskD.Size -gt 0) { [math]::Round(($diskD.Size - $diskD.FreeSpace) / $diskD.Size * 100, 1) } else { -1 }
@{ cpu=$cpu; ram=$ram; disk_c=$diskCPct; disk_d=$diskDPct } | ConvertTo-Json -Compress
"""


PS_CHECK_UPDATES = r"""
# Windows Update check — uses Microsoft Update (ServerSelection=2) so we see
# ALL Microsoft product updates (Windows, .NET, SQL, MSRT, etc.), matching
# what the Windows Settings UI shows. Without this, WSUS-managed servers
# only show WSUS-approved updates which can be a small subset.
$ErrorActionPreference = 'Stop'
try {
    $session = New-Object -ComObject Microsoft.Update.Session
    $searcher = $session.CreateUpdateSearcher()
    $searcher.ServerSelection = 2
    $r = $searcher.Search("IsInstalled=0 AND IsHidden=0")
    $results = $r.Updates
    $updates = @()
    $needsReboot = $false
    foreach ($u in $results) {
        try {
            $rebootBehavior = 0
            try { if ($u.InstallationBehavior) { $rebootBehavior = [int]$u.InstallationBehavior.RebootBehavior } } catch {}
            $rebootProp = $false
            try { $rebootProp = [bool]$u.RebootRequired } catch {}
            $reboot = $rebootProp -or ($rebootBehavior -ne 0)
            if ($reboot) { $needsReboot = $true }

            $cat = ''
            try {
                if ($u.Categories -and $u.Categories.Count -gt 0) {
                    $cat = [string]$u.Categories.Item(0).Name
                }
            } catch {}

            $kb = ''
            try {
                if ($u.KBArticleIDs -and $u.KBArticleIDs.Count -gt 0) {
                    $kb = 'KB' + [string]$u.KBArticleIDs.Item(0)
                }
            } catch {}

            $isDownloaded = $false
            try { $isDownloaded = [bool]$u.IsDownloaded } catch {}
            $status = if ($isDownloaded) { 'downloaded' } else { 'pending' }

            $size = 0
            try {
                if ($u.MaxDownloadSize) { $size = [math]::Round([double]$u.MaxDownloadSize / 1MB, 1) }
            } catch {}

            $sev = 'Unspecified'
            try { if ($u.MsrcSeverity) { $sev = [string]$u.MsrcSeverity } } catch {}

            $title = ''
            try { $title = [string]$u.Title } catch {}

            $updates += @{
                title    = $title
                severity = $sev
                kb       = $kb
                reboot   = $reboot
                category = $cat
                status   = $status
                size_mb  = $size
            }
        } catch {
            # Skip individual updates that can't be inspected — don't break the
            # whole scan for one bad entry.
            continue
        }
    }

    $pendingReboot = $false
    try {
        $sysInfo = New-Object -ComObject Microsoft.Update.SystemInfo
        if ($sysInfo) { $pendingReboot = [bool]$sysInfo.RebootRequired }
    } catch {}

    @{
        count           = $updates.Count
        updates         = $updates
        reboot_required = $needsReboot
        pending_reboot  = $pendingReboot
    } | ConvertTo-Json -Compress -Depth 4
} catch {
    @{
        count           = 0
        updates         = @()
        reboot_required = $false
        pending_reboot  = $false
        error           = [string]$_.Exception.Message
    } | ConvertTo-Json -Compress -Depth 3
}
"""


PS_COLLECT_LOGS = r"""
# Balanced log collection. Fetches a broad batch per log (200 events),
# then sorts errors/warnings to the top in PowerShell. This avoids two
# issues with the FilterHashtable approach:
#   1. Get-WinEvent -FilterHashtable throws a TERMINATING error when no
#      events match the filter, even with -ErrorAction SilentlyContinue.
#   2. The Security log doesn't use the Level field — all events are Level 0
#      (LogAlways). Audit failures are flagged via the Keywords bitmask, not
#      the Level enum. FilterHashtable Level=1,2 on Security returns nothing.
#
# So we fetch a wide batch, classify each event, sort by severity so errors
# and warnings always bubble to the top, and take the top 30 per log.
#
# Per-source channels:
#   System, Application, Security             — classic Windows event logs
#   Microsoft-Windows-Windows Firewall With
#     Advanced Security/Firewall              — Windows Firewall events
#                                              (rule changes, service stops,
#                                              policy issues, app blocks)
#
# The firewall channel is treated as a separate source so the UI can show
# it in a dedicated "Firewall Logs" panel. We drop event IDs 5152/5153
# (every allow/deny packet decision) because they're extremely high
# volume on busy servers and would crowd out actually-interesting policy
# events. Operators who need full packet logging should enable pfirewall.log
# via Group Policy separately.
$levelMap = @{ 1='Critical'; 2='Error'; 3='Warning'; 4='Information'; 5='Information'; 0='Information' }
# Keyword bitmasks for Security audit events (unsigned values)
$AUDIT_FAILURE = [long]0x10000000000000

# Firewall channel name + the "boring" event IDs we explicitly skip.
$FIREWALL_LOG = 'Microsoft-Windows-Windows Firewall With Advanced Security/Firewall'
$FIREWALL_NOISE_IDS = @(5152, 5153)  # packet drop / allow notifications

# Display name → channel name. We store the display name in the DB so the
# UI can filter without dealing with the long Microsoft-Windows-... path.
$channels = @(
    @{ display = 'System';      log = 'System' }
    @{ display = 'Application'; log = 'Application' }
    @{ display = 'Security';    log = 'Security' }
    @{ display = 'Firewall';    log = $FIREWALL_LOG }
)

$logs = @()
foreach ($ch in $channels) {
    $displayName = $ch.display
    $logName = $ch.log
    $events = $null
    try {
        $events = @(Get-WinEvent -LogName $logName -MaxEvents 200 -ErrorAction SilentlyContinue)
    } catch { $events = @() }
    if (-not $events -or $events.Count -eq 0) { continue }

    # Firewall channel: drop the very high-volume per-packet events. Keep
    # everything else so policy changes / blocked apps / service stops
    # survive into the response.
    if ($displayName -eq 'Firewall') {
        $events = @($events | Where-Object { $_ -and ($FIREWALL_NOISE_IDS -notcontains [int]$_.Id) })
        if ($events.Count -eq 0) { continue }
    }

    # Classify each event into a sort-priority bucket so we always surface
    # errors and warnings even when they're outnumbered 100:1 by info noise.
    $buckets = @{0=@(); 1=@(); 2=@(); 3=@()}
    foreach ($e in $events) {
        if (-not $e) { continue }
        $lvl = [int]$e.Level
        if ($displayName -eq 'Security') {
            # Security events: audit failures → bucket 1 (Error), rest → bucket 3 (Info)
            try {
                if ([long]$e.Keywords -band $AUDIT_FAILURE) { $buckets[1] += $e }
                else { $buckets[3] += $e }
            } catch { $buckets[3] += $e }
        } else {
            # System / Application / Firewall: bucket by standard Level
            if ($lvl -le 2 -and $lvl -ge 1) { $buckets[0] += $e }      # Critical / Error
            elseif ($lvl -eq 3)             { $buckets[1] += $e }      # Warning
            else                            { $buckets[3] += $e }      # Info / LogAlways
        }
    }

    # Take top-N from each bucket: 15 critical/error, 10 warning, 5 info
    $selected = @()
    $selected += @($buckets[0] | Select-Object -First 15)
    $selected += @($buckets[1] | Select-Object -First 10)
    $selected += @($buckets[2] | Select-Object -First 5)
    if ($selected.Count -lt 30) {
        $selected += @($buckets[3] | Select-Object -First (30 - $selected.Count))
    }

    foreach ($e in $selected) {
        if (-not $e) { continue }
        $rawLvl = [int]$e.Level
        # For Security audit failures, override the level string
        $lvlStr = 'Information'
        if ($displayName -eq 'Security') {
            try {
                if ([long]$e.Keywords -band $AUDIT_FAILURE) { $lvlStr = 'Error' }
                else { $lvlStr = 'Information' }
            } catch {}
        } else {
            $lvlStr = if ($levelMap.ContainsKey($rawLvl)) { $levelMap[$rawLvl] } else { 'Information' }
        }
        $msg = ''
        try {
            $msg = [string]$e.Message
            if ($msg.Length -gt 200) { $msg = $msg.Substring(0,200) }
        } catch {}
        $tstr = ''
        try { $tstr = $e.TimeCreated.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ') } catch {}
        $logs += @{
            source   = $displayName
            time     = $tstr
            level    = $lvlStr
            event_id = $e.Id
            message  = $msg
        }
    }
}
# Force array shape so ConvertTo-Json emits [] (not null) for empty / 1-item
,$logs | ConvertTo-Json -Compress -Depth 3
"""


PS_HARDWARE_SCRIPT = r"""
$proc = Get-CimInstance Win32_Processor
$cores = ($proc | Measure-Object -Property NumberOfCores -Sum).Sum
$threads = ($proc | Measure-Object -Property NumberOfLogicalProcessors -Sum).Sum
$cpuName = ($proc | Select-Object -First 1).Name
$os = Get-CimInstance Win32_OperatingSystem
$totalRamGB = [math]::Round($os.TotalVisibleMemorySize / 1MB, 1)
$osCaption = $os.Caption
# DriveType=3 = fixed local disks only (mirrors PS_COLLECT_SCRIPT): an optical
# or removable drive at C:/D: is not real server storage, so it reports -1 here
# too and the hardware panel won't list a DVD as a data volume.
$diskC = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='C:' AND DriveType=3"
$diskCSizeGB = if ($diskC -and $diskC.Size -gt 0) { [math]::Round($diskC.Size / 1GB, 1) } else { -1 }
$diskCFreeGB = if ($diskC -and $diskC.Size -gt 0) { [math]::Round($diskC.FreeSpace / 1GB, 1) } else { -1 }
$diskD = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='D:' AND DriveType=3"
$diskDSizeGB = if ($diskD -and $diskD.Size -gt 0) { [math]::Round($diskD.Size / 1GB, 1) } else { -1 }
$diskDFreeGB = if ($diskD -and $diskD.Size -gt 0) { [math]::Round($diskD.FreeSpace / 1GB, 1) } else { -1 }
@{
    cpu_name=$cpuName; cores=$cores; threads=$threads;
    total_ram_gb=$totalRamGB; os=$osCaption;
    disk_c_size_gb=$diskCSizeGB; disk_c_free_gb=$diskCFreeGB;
    disk_d_size_gb=$diskDSizeGB; disk_d_free_gb=$diskDFreeGB
} | ConvertTo-Json -Compress
"""


PS_COLLECT_FAILED_LOGINS = r"""
# Collect failed login + lockout events (4625, 4740) from the Security log.
# Enrichment strategy for the "calling process / service" field:
#  1. If ProcessName is present in event XML AND we can still find the PID,
#     try Get-Process and Win32_Service to resolve it to a friendly name.
#  2. If process can't be resolved (most network logons — Windows doesn't put a
#     process in 4625 for NTLM/Kerberos auth that originates remotely), fall
#     back to LogonProcessName + AuthenticationPackageName which Windows DOES
#     emit for every 4625. Examples:
#        LogonProcessName="NtLmSsp"    AuthPackage="NTLM"     -> "NTLM via SMB/RPC"
#        LogonProcessName="Kerberos"   AuthPackage="Kerberos" -> "Kerberos (AD)"
#        LogonProcessName="Advapi  "   AuthPackage="Negotiate"-> "Local API call (RunAs / service start)"
#        LogonProcessName="User32 "    AuthPackage="Negotiate"-> "Interactive logon"
#        LogonProcessName="seclogo"               -> "Secondary Logon (RunAs)"
#  3. If still nothing, leave empty.
$cutoff = (Get-Date).AddMinutes(-15)
try {
    $events = Get-WinEvent -FilterHashtable @{LogName='Security'; Id=4625,4740; StartTime=$cutoff} -ErrorAction SilentlyContinue
} catch { $events = @() }
$lt = @{2='Interactive';3='Network';4='Batch';5='Service';7='Unlock';8='NetworkCleartext';9='NewCredentials';10='RDP';11='CachedInteractive'}

# Friendly labels for known LogonProcessName values
$lpnFriendly = @{
    'NtLmSsp '   = 'NTLM (SMB/RPC/network share)'
    'Kerberos'   = 'Kerberos (Active Directory)'
    'Advapi  '   = 'Local API call (RunAs / service start)'
    'User32 '    = 'Interactive logon (User32)'
    'seclogo'    = 'Secondary Logon (RunAs)'
    'IKE'        = 'IKE / IPsec'
    'CredSSP'    = 'CredSSP (RDP / WinRM)'
    'SshdPipe'   = 'OpenSSH server'
}

$results = @()
foreach ($evt in $events) {
    $xml = [xml]$evt.ToXml()
    $d = $xml.Event.EventData.Data
    $gv = { param($n) ($d | Where-Object { $_.Name -eq $n }).'#text' }
    $lti = 0; try { $lti = [int](& $gv 'LogonType') } catch {}
    $procLabel = ''

    # Step 1 — try to resolve the actual calling process via PID
    $pn = & $gv 'ProcessName'
    if ($pn -and $pn -ne '-') {
        # Default to the bare path (or just the exe name)
        try { $procLabel = (Split-Path $pn -Leaf 2>$null) } catch { $procLabel = $pn }
        if (-not $procLabel) { $procLabel = $pn }
        # Try richer resolution if PID is still alive
        try {
            $procPid = [int](& $gv 'ProcessId')
            if ($procPid -gt 0) {
                $svc = Get-WmiObject Win32_Service -Filter "ProcessId=$procPid" -ErrorAction SilentlyContinue
                if ($svc) {
                    $procLabel = "$($svc.DisplayName) [$($svc.Name)] (PID $procPid)"
                } else {
                    $proc = Get-Process -Id $procPid -ErrorAction SilentlyContinue
                    if ($proc) { $procLabel = "$($proc.ProcessName) (PID $procPid)" }
                }
            }
        } catch {}
    }

    # Step 2 — fall back to LogonProcessName + AuthenticationPackageName when
    # no usable ProcessName (typical for network logons of type 3)
    if (-not $procLabel) {
        $lpn = & $gv 'LogonProcessName'
        $apn = & $gv 'AuthenticationPackageName'
        if ($lpn -or $apn) {
            $lpnTrim = if ($lpn) { $lpn.TrimEnd() } else { '' }
            $friendly = if ($lpn -and $lpnFriendly.ContainsKey($lpn)) { $lpnFriendly[$lpn] } `
                        elseif ($lpnTrim -and $lpnFriendly.ContainsKey($lpnTrim)) { $lpnFriendly[$lpnTrim] } `
                        else { '' }
            if ($friendly) {
                $procLabel = $friendly
                if ($apn -and $apn -ne $lpnTrim) { $procLabel = "$friendly ($apn)" }
            } elseif ($lpnTrim) {
                $procLabel = if ($apn) { "$lpnTrim / $apn" } else { $lpnTrim }
            }
        }
    }

    $results += @{
        timestamp    = $evt.TimeCreated.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        event_id     = $evt.Id
        account_name = if (& $gv 'TargetUserName') { & $gv 'TargetUserName' } else { '' }
        domain       = if (& $gv 'TargetDomainName') { & $gv 'TargetDomainName' } else { '' }
        source_ip    = if (& $gv 'IpAddress') { & $gv 'IpAddress' } else { '' }
        source_port  = if (& $gv 'IpPort') { & $gv 'IpPort' } else { '' }
        logon_type   = if ($lt[$lti]) { $lt[$lti] } else { "Type $lti" }
        workstation  = if (& $gv 'WorkstationName') { & $gv 'WorkstationName' } else { '' }
        status_code  = if (& $gv 'Status') { & $gv 'Status' } else { '' }
        sub_status   = if (& $gv 'SubStatus') { & $gv 'SubStatus' } else { '' }
        process_name = if ($procLabel) { $procLabel } else { '' }
    }
}
$results | ConvertTo-Json -Compress
"""
