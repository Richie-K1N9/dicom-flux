# dicom.flux

A portable single-file DICOM tester for Windows and Linux. Sends and receives
C-ECHO, Modality Worklist (C-FIND), C-STORE, and DICOM Print, with a built-in
SCP listener so two instances can talk to each other on the network.

## Features

- **Echo** — verify connectivity (C-ECHO).
- **Modality Worklist** — query a worklist provider (C-FIND) with patient /
  accession / date / modality / AE filters.
- **Send DICOM** — built-in samples (CT, MR, CR, DX, US, MG) or upload your own
  `.dcm` / `.zip`. Drag a file onto the Source box.
- **Print** — send a print job (Basic Grayscale Print Management); probes the
  printer for available media via N-GET.
- **Built-in SCP** — always-on listener accepts Echo, MWL, all standard storage
  SOP classes, and grayscale / color print management. Worklist replies use
  randomly generated dummy data re-rolled at each launch.
- **Data tab** — double-click a received DICOM file to open the built-in viewer
  (window/level, multi-frame scroll); double-click a print job to see a film
  preview with header / footer metadata.
- **Settings** — local AE title and port, theme (Black / Dark / Light), and an
  optional path to Weasis to enable an *Open in Weasis* button in the viewer.

## Run from source

```bash
python -m venv .venv
# Windows:  .venv\Scripts\Activate.ps1
# Linux:    source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

## Build a single-file executable

Re-generate the multi-resolution Windows icon once whenever
`assets/dicom_flux.svg` changes:

```bash
python build_icon.py
```

### Windows (`.exe`)

PowerShell (line continuation with backtick):

```powershell
pip install pyinstaller
pyinstaller --onefile --windowed `
            --icon assets\dicom_flux.ico `
            --add-data "assets\dicom_flux.svg;assets" `
            --name "[dicom.flux]" main.py
```

Command Prompt (line continuation with caret):

```bat
pip install pyinstaller
pyinstaller --onefile --windowed ^
            --icon assets\dicom_flux.ico ^
            --add-data "assets\dicom_flux.svg;assets" ^
            --name "[dicom.flux]" main.py
```

The binary is written to `dist\[dicom.flux].exe`.

### Linux (single ELF binary)

```bash
pip install pyinstaller
pyinstaller --onefile \
            --add-data "assets/dicom_flux.svg:assets" \
            --name "[dicom.flux]" main.py
```

The binary is written to `dist/[dicom.flux]`. Build on the target distro/glibc
you intend to run on. Linux executables don't carry a file icon — use a
`.desktop` entry pointing at `assets/dicom_flux.svg` for a launcher icon.

## Demos

### Echo (C-ECHO)

![Sending a C-ECHO request](assets/demos/echo.gif)

### Modality Worklist (C-FIND)

![Querying a Modality Worklist provider](assets/demos/worklist.gif)

### Send DICOM and Print

![Sending a DICOM C-STORE and a print job](assets/demos/send-and-print.gif)
