<img width="3388" height="1512" alt="ReleaseTC" src="https://github.com/user-attachments/assets/c7504fdf-0048-454c-9d61-2d0879c3a02a" />
# GMod MMD Motion Importer
Visit: https://steamcommunity.com/sharedfiles/filedetails/?id=3718373357

Qt-based Windows importer for converting MMD `.vmd` motions into motion JSON files used by the `mmd_vmd_npc` Garry's Mod addon. The importer can preview PMX/VMD (with camera) motion, bake IK through Blender, merge optional face/flex VMDs, convert audio/video to GMod MP3 sound, and optionally package an imported motion as a `.gma` addon.

Please Update to the latest version of the motion importer. 

## Repository Layout

```text
mmd_vmd_npc/                 Garry's Mod addon source
source_models/mmd_model/     Bundled PMX model and textures used for baking/preview
tools/import_vmd.py          CLI importer and cache writer
tools/mmd_vmd_importer_gui.py Qt desktop importer UI
tools/blender_bake_vmd.py    Blender-side bake/export script
tools/preview/               OpenGL PMX/VMD preview renderer
tools/i18n/                  Importer UI translation files
tools/build_importer_exe.ps1 PyInstaller build script
tools/requirements_importer.txt Python dependencies
```

## Requirements

- Windows 10/11, 64-bit.
- Python 3.10 or newer, 64-bit. Python from python.org is recommended for PyInstaller builds.
- Blender 4.3 or newer.
- Garry's Mod installed through Steam.
- PowerShell for the EXE build script.

The importer auto-detects Steam Garry's Mod and Steam Blender when possible. Blender is still required even when using the packaged EXE, because the importer runs Blender in the background to bake MMD IK/constraints.

## Set Up From Source

Open PowerShell in the repository root:

```powershell
cd C:\path\to\enhanced_animation_importer_arc
```

Create and activate a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
```

Install importer dependencies:

```powershell
python -m pip install -r tools/requirements_importer.txt
```

Run the importer UI:

```powershell
python tools/mmd_vmd_importer_gui.py
```

## Build The Windows EXE

Build the standard one-file importer:

```powershell
powershell -ExecutionPolicy Bypass -File tools/build_importer_exe.ps1 -Python .\.venv\Scripts\python.exe
```

The output is:

```text
dist/GmodMMDMotionImporter.exe
```

Build without bundled ffmpeg to reduce file size:

```powershell
powershell -ExecutionPolicy Bypass -File tools/build_importer_exe.ps1 -Python .\.venv\Scripts\python.exe -NoBundledFFmpeg
```

With `-NoBundledFFmpeg`, music/video import still works only if `ffmpeg.exe` is available on `PATH`.

Optional build switches:

```powershell
# Change output EXE name
powershell -ExecutionPolicy Bypass -File tools/build_importer_exe.ps1 -Name MyImporter

# Allow UPX compression if UPX is installed and available
powershell -ExecutionPolicy Bypass -File tools/build_importer_exe.ps1 -UseUPX
```

The build script bundles:

- `tools/mmd_vmd_importer_gui.py`
- `tools/import_vmd.py`
- `tools/blender_bake_vmd.py`
- `tools/preview/`
- `tools/i18n/`
- `tools/assets/`
- `source_models/mmd_model/`
- mapping files from `source_models/`
- the `mmd_vmd_npc/` addon files
- ffmpeg from `imageio-ffmpeg`, unless `-NoBundledFFmpeg` is used

## Using The Importer UI

1. Select a body motion `.vmd`.
2. Optionally add face/flex VMDs.
3. Optionally select a music or video file. The importer extracts/encodes audio to MP3.
4. Preview the motion and adjust audio offset if needed.
5. Click the bake/import button.

The importer writes motion JSON to:

```text
<GarrysMod>/garrysmod/data/mmd_vmd_npc/motions/
```

Imported music is written to:

```text
<GarrysMod>/garrysmod/sound/mmd_vmd_npc/music/
```

The UI remembers Blender path, Garry's Mod path, language choice, and recent inputs between sessions.

## CLI Usage

Print detected paths:

```powershell
python tools/import_vmd.py --print-gmod
python tools/import_vmd.py --print-blender
```

Bake and import a body VMD:

```powershell
python tools/import_vmd.py "motions\dance.vmd" --bake-with-blender --cache
```

Import with extra flex VMDs and music:

```powershell
python tools/import_vmd.py "motions\dance.vmd" `
  --bake-with-blender `
  --cache `
  --flex-vmd "motions\face.vmd" `
  --flex-vmd "motions\mouth.vmd" `
  --music "music\dance.wav" `
  --audio-offset -0.25 `
  --motion-name "My Dance"
```

Override detected paths:

```powershell
python tools/import_vmd.py "motions\dance.vmd" `
  --bake-with-blender `
  --cache `
  --blender "C:\Program Files (x86)\Steam\steamapps\common\Blender\blender.exe" `
  --gmod-dir "H:\SteamLibrary\steamapps\common\GarrysMod"
```

Also export the imported motion as a `.gma`:

```powershell
python tools/import_vmd.py "motions\dance.vmd" `
  --bake-with-blender `
  --cache `
  --motion-name "My Dance" `
  --export-addon-gma "dist\MyDance.gma"
```

## Blender And MMD Tools

The bake helper imports the bundled PMX model, imports the VMD motion, bakes pose animation with visual keying, exports a baked VMD, then exports parent-corrected JSON data for GMod.

If `mmd_tools` is not available in Blender, the helper tries Blender 4.x extension module names and then attempts to install the official MMD Tools extension from:

```text
https://extensions.blender.org/add-ons/mmd-tools/
```

Baking requires Blender 4.3 or newer. Blender 4.2 or older is rejected.

## Troubleshooting

### `_ctypes` or `DLL load failed` when running the EXE

Use the latest `tools/build_importer_exe.ps1`. It explicitly includes Python DLL paths and common runtime DLLs such as libffi. If the issue persists, rebuild using a normal 64-bit CPython install instead of the embeddable Python distribution.

### PyInstaller is missing

Install the requirements again:

```powershell
python -m pip install -r tools/requirements_importer.txt
```

### The EXE is too large

Build without bundled ffmpeg:

```powershell
powershell -ExecutionPolicy Bypass -File tools/build_importer_exe.ps1 -NoBundledFFmpeg
```

The user will need ffmpeg on `PATH` for music/video conversion.

### Blender cannot import MMD files

Open Blender once and verify MMD Tools can be enabled. The importer can attempt automatic extension install, but local Blender permissions or network restrictions can block it.

### Garry's Mod is not detected

Pass the path manually in the UI, or use CLI:

```powershell
python tools/import_vmd.py "motions\dance.vmd" --gmod-dir "H:\SteamLibrary\steamapps\common\GarrysMod" --bake-with-blender --cache
```
