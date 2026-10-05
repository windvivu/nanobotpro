# Open a dashboard URL in the default browser as soon as its port answers (gives up after 2 min).
# Started in the background by nanobot-launcher.ps1 and nanobot-single.ps1.
# Set NANOBOT_NO_BROWSER=1 to skip, e.g. when the bot starts with Windows.
param([Parameter(Mandatory = $true)][string]$Url)

if ($env:NANOBOT_NO_BROWSER -eq "1") { exit 0 }
$uri = [Uri]$Url
$deadline = (Get-Date).AddMinutes(2)
while ((Get-Date) -lt $deadline) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $client.Connect($uri.Host, $uri.Port)
        Start-Process $Url
        exit 0
    } catch {
        Start-Sleep -Milliseconds 500
    } finally {
        $client.Dispose()
    }
}
