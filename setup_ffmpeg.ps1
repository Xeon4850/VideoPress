$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Install-Build($folder, $url, $label) {
    try {
        $targetBin = Join-Path $root "$folder\bin"
        if ((Test-Path (Join-Path $targetBin "ffmpeg.exe")) -and (Test-Path (Join-Path $targetBin "ffprobe.exe"))) {
            Write-Host "$label already exists; keeping it."
            return $true
        }
        $zip = Join-Path $env:TEMP "videopress-$folder.zip"
        $extract = Join-Path $env:TEMP ("videopress-" + [guid]::NewGuid().ToString())
        Write-Host "Downloading $label from $url"
        Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
        Expand-Archive -Path $zip -DestinationPath $extract -Force
        $bin = Get-ChildItem -Path $extract -Directory | Select-Object -First 1 | ForEach-Object { Join-Path $_.FullName "bin" }
        if (-not (Test-Path (Join-Path $bin "ffmpeg.exe"))) { throw "ffmpeg.exe missing" }
        if (-not (Test-Path (Join-Path $bin "ffprobe.exe"))) { throw "ffprobe.exe missing" }
        if (Test-Path $targetBin) { Remove-Item $targetBin -Recurse -Force }
        New-Item -ItemType Directory -Path (Split-Path $targetBin -Parent) -Force | Out-Null
        Copy-Item -Path $bin -Destination $targetBin -Recurse -Force
        Remove-Item $zip -Force -ErrorAction SilentlyContinue
        Remove-Item $extract -Recurse -Force -ErrorAction SilentlyContinue
        & (Join-Path $targetBin "ffmpeg.exe") -version | Select-Object -First 1 | Write-Host
        return $true
    }
    catch {
        Write-Host "$label failed: $($_.Exception.Message)"
        return $false
    }
}

$latestOK = Install-Build "ffmpeg-latest" "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" "Latest FFmpeg"
$compatOK = Install-Build "ffmpeg-compat" "https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-8.1.2-essentials_build.zip" "FFmpeg 8.1.2 GTX 1080 compatible"
if (-not $latestOK -and -not $compatOK) { throw "Both FFmpeg downloads failed." }
Write-Host "At least one FFmpeg build is ready."
