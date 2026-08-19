param(
    [string]$Python = "python",
    [string]$Name = "GmodMMDMotionImporter",
    [switch]$UseUPX,
    [switch]$NoBundledFFmpeg,
    # Blender 4.5.10 LTS is embedded in the one-file EXE. At runtime the importer
    # reuses the Simple Character Model Importer's Blender if present, otherwise it
    # extracts this embedded zip once into %LOCALAPPDATA%\MMDVMDNPC. Pass -BlenderZip
    # to point at an already-downloaded portable zip; otherwise the script reuses a
    # local copy (SCMI download cache / project) or downloads the pinned version.
    [string]$BlenderZip = "",
    [string]$BlenderVersion = "4.5.10",
    [switch]$NoBundledBlender,
    # mmd_tools is REQUIRED in the bundle so a fresh machine bakes fully offline:
    # a first-run bake that must reach extensions.blender.org fails outright on
    # machines with broken/expired certificate stores. Resolution order:
    # -MmdToolsZip / env MMDVMDNPC_MMD_TOOLS_ZIP -> newest vendored
    # tools\blender_addons\*mmd*tools*.zip -> pinned download from the
    # extensions API into build\mmd_tools_cache. The build FAILS if none
    # resolves; pass -NoBundledMmdTools to knowingly build an exe that needs
    # internet for its first bake.
    [string]$MmdToolsZip = "",
    [switch]$NoBundledMmdTools
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Push-Location $Root
try {
    $IconPath = Join-Path $Root "tools\assets\importer_icon.ico"
    $PythonPrefix = (& $Python -c "import sys; print(sys.prefix)")
    if ($LASTEXITCODE -ne 0) {
        throw "Could not resolve Python prefix"
    }
    $LibraryBin = Join-Path $PythonPrefix "Library\bin"
    $PythonDlls = Join-Path $PythonPrefix "DLLs"
    $ExtraArgs = @()
    function Add-BinaryIfExists([string]$Path, [string]$Dest = ".") {
        if (Test-Path $Path) {
            $script:ExtraArgs += @("--add-binary", "$Path;$Dest")
            return $true
        }
        return $false
    }

    if (Test-Path $LibraryBin) {
        $ExtraArgs += @("--paths", $LibraryBin)
        foreach ($Dll in @(
            "ffi-8.dll",
            "libffi-8.dll",
            "libssl-3-x64.dll",
            "libcrypto-3-x64.dll",
            "libexpat.dll",
            "liblzma.dll",
            "libbz2.dll",
            "zlib.dll",
            "sqlite3.dll"
        )) {
            [void](Add-BinaryIfExists (Join-Path $LibraryBin $Dll))
        }
    }
    if (Test-Path $PythonDlls) {
        $ExtraArgs += @("--paths", $PythonDlls)
    }
    foreach ($Dll in @("zlib.dll")) {
        [void](Add-BinaryIfExists (Join-Path $PythonPrefix $Dll))
    }
    if (-not $NoBundledFFmpeg) {
        $FfmpegPath = (& $Python -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())")
        if ($LASTEXITCODE -eq 0 -and (Test-Path $FfmpegPath)) {
            $ExtraArgs += @("--add-binary", "$FfmpegPath;imageio_ffmpeg\binaries")
        }
    }

    # Embed portable Blender 4.5.10 so the importer needs no separate install.
    if (-not $NoBundledBlender) {
        $BlenderZipName = "blender-$BlenderVersion-windows-x64.zip"
        $ResolvedBlenderZip = ""
        $Candidates = @()
        if ($BlenderZip) { $Candidates += $BlenderZip }
        if ($env:MMDVMDNPC_BLENDER_ZIP) { $Candidates += $env:MMDVMDNPC_BLENDER_ZIP }
        if ($env:LOCALAPPDATA) {
            # The Simple Character Model Importer keeps the same portable zip here.
            $Candidates += (Join-Path $env:LOCALAPPDATA "MMDCharacterImporter\downloads\$BlenderZipName")
        }
        $Candidates += (Join-Path $Root "build\blender_cache\$BlenderZipName")
        foreach ($cand in $Candidates) {
            if ($cand -and (Test-Path $cand) -and ((Get-Item $cand).Length -gt 50MB)) {
                $ResolvedBlenderZip = (Resolve-Path $cand).Path
                break
            }
        }
        if (-not $ResolvedBlenderZip) {
            $CacheDir = Join-Path $Root "build\blender_cache"
            New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null
            $DownloadTarget = Join-Path $CacheDir $BlenderZipName
            $Series = ($BlenderVersion -split '\.')[0..1] -join '.'
            $Url = "https://download.blender.org/release/Blender$Series/$BlenderZipName"
            Write-Host "Downloading Blender $BlenderVersion (~400 MB) from $Url ..."
            $PrevProgress = $ProgressPreference
            $ProgressPreference = 'SilentlyContinue'
            try {
                Invoke-WebRequest -Uri $Url -OutFile $DownloadTarget
            }
            finally {
                $ProgressPreference = $PrevProgress
            }
            if (-not (Test-Path $DownloadTarget) -or (Get-Item $DownloadTarget).Length -lt 50MB) {
                throw "Blender download failed or is too small: $DownloadTarget"
            }
            $ResolvedBlenderZip = $DownloadTarget
        }
        # PyInstaller keeps the source basename, and the runtime only recognises a
        # `blender-*-windows-x64.zip` name, so normalise a custom -BlenderZip to the
        # canonical filename before embedding.
        if ([System.IO.Path]::GetFileName($ResolvedBlenderZip) -ne $BlenderZipName) {
            $StageDir = Join-Path $Root "build\blender_cache"
            New-Item -ItemType Directory -Force -Path $StageDir | Out-Null
            $CanonicalPath = Join-Path $StageDir $BlenderZipName
            if ((Resolve-Path $ResolvedBlenderZip).Path -ne $CanonicalPath) {
                Copy-Item -LiteralPath $ResolvedBlenderZip -Destination $CanonicalPath -Force
            }
            $ResolvedBlenderZip = $CanonicalPath
        }
        Write-Host "Embedding Blender: $ResolvedBlenderZip"
        $ExtraArgs += @("--add-data", "$ResolvedBlenderZip;blender")
    }
    else {
        Write-Host "Skipping bundled Blender (-NoBundledBlender); the importer will reuse a sibling/system Blender at runtime."
    }

    # Embed an mmd_tools zip for fully-offline first-run baking. Mandatory
    # unless -NoBundledMmdTools: without it the exe depends on internet (and a
    # working certificate store) for its very first bake on a fresh machine.
    if (-not $NoBundledMmdTools) {
        $MmdToolsSource = if ($MmdToolsZip) { $MmdToolsZip } elseif ($env:MMDVMDNPC_MMD_TOOLS_ZIP) { $env:MMDVMDNPC_MMD_TOOLS_ZIP } else { "" }
        if ($MmdToolsSource -and -not (Test-Path $MmdToolsSource)) {
            throw "mmd_tools zip not found: $MmdToolsSource"
        }
        if (-not $MmdToolsSource) {
            $Vendored = Get-ChildItem (Join-Path $Root "tools\blender_addons") -Filter "*mmd*tools*.zip" -ErrorAction SilentlyContinue |
                Sort-Object Name -Descending | Select-Object -First 1
            if ($Vendored) { $MmdToolsSource = $Vendored.FullName }
        }
        if (-not $MmdToolsSource) {
            $MmdCacheDir = Join-Path $Root "build\mmd_tools_cache"
            New-Item -ItemType Directory -Force $MmdCacheDir | Out-Null
            Write-Host "No vendored mmd_tools zip; downloading the pinned extension for Blender $BlenderVersion ..."
            try {
                $Api = Invoke-RestMethod -Uri "https://extensions.blender.org/api/v1/extensions/?blender_version=$BlenderVersion" -Headers @{ "User-Agent" = "mmd-vmd-importer-build" } -TimeoutSec 90
                $Mmd = $Api.data | Where-Object { $_.id -eq "mmd_tools" } | Select-Object -First 1
                if ($Mmd) {
                    $MmdTarget = Join-Path $MmdCacheDir "mmd_tools-$($Mmd.version).zip"
                    Invoke-WebRequest -Uri $Mmd.archive_url -OutFile $MmdTarget -Headers @{ "User-Agent" = "mmd-vmd-importer-build" } -TimeoutSec 300
                    if ((Test-Path $MmdTarget) -and (Get-Item $MmdTarget).Length -gt 100KB) { $MmdToolsSource = $MmdTarget }
                }
            }
            catch {
                Write-Host "mmd_tools download failed: $_"
            }
        }
        if (-not $MmdToolsSource -or -not (Test-Path $MmdToolsSource)) {
            throw "Could not resolve an mmd_tools zip to embed; the exe would need internet on its first bake. Vendor one at tools\blender_addons\, pass -MmdToolsZip, or pass -NoBundledMmdTools to build without it."
        }
        $ResolvedMmd = (Resolve-Path $MmdToolsSource).Path
        Write-Host "Embedding mmd_tools archive: $ResolvedMmd"
        $ExtraArgs += @("--add-data", "$ResolvedMmd;blender_addons")
    }
    else {
        Write-Host "Skipping bundled mmd_tools (-NoBundledMmdTools); first-run bakes will require internet."
    }

    & $Python -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('PyInstaller') else 1)" > $null
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller is not installed. Run: $Python -m pip install pyinstaller"
    }

    $OutputExe = Join-Path $Root "dist\$Name.exe"
    if (Test-Path $OutputExe) {
        try {
            Remove-Item -LiteralPath $OutputExe -Force
        }
        catch {
            throw "Could not replace dist\$Name.exe. Close any running importer window using this EXE, then build again. $($_.Exception.Message)"
        }
    }

    $OptimizedImports = @(
        "--hidden-import", "ctypes",
        "--hidden-import", "_ctypes",
        "--hidden-import", "numpy",
        "--hidden-import", "OpenGL.GL",
        "--hidden-import", "OpenGL.arrays.numpymodule",
        "--hidden-import", "OpenGL.platform.win32",
        "--hidden-import", "PySide6.QtCore",
        "--hidden-import", "PySide6.QtGui",
        "--hidden-import", "PySide6.QtWidgets",
        "--hidden-import", "PySide6.QtMultimedia",
        "--hidden-import", "PySide6.QtOpenGLWidgets"
    )
    if (-not $NoBundledFFmpeg) {
        $OptimizedImports += @(
            "--hidden-import", "imageio_ffmpeg",
            "--copy-metadata", "imageio_ffmpeg"
        )
    }

    $ExcludedModules = @(
        "IPython",
        "OpenGL_accelerate",
        "OpenGL.GLE",
        "OpenGL.GLUT",
        "OpenGL.Tk",
        "PIL",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "matplotlib",
        "pandas",
        "pytest",
        "scipy",
        "setuptools",
        "tkinter",
        "torch",
        "unittest",
        "cv2",
        "PySide6.Qt3DAnimation",
        "PySide6.Qt3DCore",
        "PySide6.Qt3DExtras",
        "PySide6.Qt3DInput",
        "PySide6.Qt3DLogic",
        "PySide6.Qt3DRender",
        "PySide6.QtBluetooth",
        "PySide6.QtCharts",
        "PySide6.QtDataVisualization",
        "PySide6.QtDesigner",
        "PySide6.QtHelp",
        "PySide6.QtLocation",
        "PySide6.QtNetworkAuth",
        "PySide6.QtPdf",
        "PySide6.QtPdfWidgets",
        "PySide6.QtPositioning",
        "PySide6.QtPrintSupport",
        "PySide6.QtQml",
        "PySide6.QtQuick",
        "PySide6.QtQuick3D",
        "PySide6.QtQuickControls2",
        "PySide6.QtQuickWidgets",
        "PySide6.QtRemoteObjects",
        "PySide6.QtScxml",
        "PySide6.QtSensors",
        "PySide6.QtSerialPort",
        "PySide6.QtSql",
        "PySide6.QtSvg",
        "PySide6.QtTest",
        "PySide6.QtTextToSpeech",
        "PySide6.QtWebChannel",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineQuick",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebSockets",
        "PySide6.QtXml"
    )
    foreach ($Module in $ExcludedModules) {
        $ExtraArgs += @("--exclude-module", $Module)
    }
    if ($NoBundledFFmpeg) {
        $ExtraArgs += @("--exclude-module", "imageio_ffmpeg")
    }
    if (-not $UseUPX) {
        $ExtraArgs += @("--noupx")
    }

    # PyInstaller warns, and future versions may block, when launched from an elevated token.
    # This build script intentionally supports admin terminals, so bypass only that guard.
    $RunPyInstaller = "import sys; import PyInstaller.__main__ as pyi; pyi.check_unsafe_privileges = lambda: None; pyi.run(sys.argv[1:])"
    & $Python -c $RunPyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --windowed `
        --name $Name `
        --icon "$IconPath" `
        @OptimizedImports `
        --add-data "mmd_vmd_npc;mmd_vmd_npc" `
        --add-data "source_models\mmd_model;source_models\mmd_model" `
        --add-data "source_models\bone_mmd_to_source.py;source_models" `
        --add-data "source_models\flex_mmd_to_source.py;source_models" `
        --add-data "source_models\mmd_model_source_format\Body.smd;source_models\mmd_model_source_format" `
        --add-data "tools\assets;tools\assets" `
        --add-data "tools\i18n;tools\i18n" `
        --add-data "tools\preview;tools\preview" `
        --add-data "tools\blender_bake_vmd.py;tools" `
        --add-data "tools\import_vmd.py;tools" `
        @ExtraArgs `
        "tools\mmd_vmd_importer_gui.py"

    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller build failed"
    }

    if (Test-Path $OutputExe) {
        $SizeMb = [math]::Round((Get-Item $OutputExe).Length / 1MB, 1)
        Write-Host "Built dist\$Name.exe ($SizeMb MB)"
        Write-Host "Size optimization: targeted Qt/OpenGL/imageio imports are used instead of --collect-all package bundling."
        if ($NoBundledFFmpeg) {
            Write-Host "FFmpeg was not bundled; music/video conversion requires ffmpeg on PATH."
        }
    }
    else {
        Write-Host "Built dist\$Name.exe"
    }
}
finally {
    Pop-Location
}
