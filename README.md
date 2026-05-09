# dicom.flux

A portable single-file DICOM tester for Windows and Linux. Supports C-ECHO,
Modality Worklist (C-FIND), DICOM C-STORE, and DICOM Print (Basic Grayscale
Print Management). Includes a built-in SCP listener so two instances can talk
to each other on the same network.

## Run from source

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    Linux: source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

## Build a single-file executable

The build embeds the application icon (rendered from
`assets/dicom_flux.svg`). Re-generate the multi-resolution `.ico` once
whenever the SVG changes:

```bash
python build_icon.py
```

### Windows (`.exe`)

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
you intend to run on. (Linux executables don't carry a file icon — use a
`.desktop` entry pointing at `assets/dicom_flux.svg` if you want a launcher
icon.)

## Tests provided

| Tab                 | DIMSE        | Notes                                                      |
|---------------------|--------------|------------------------------------------------------------|
| Echo                | C-ECHO       | Verify connectivity and AE-title acceptance.               |
| Modality Work List  | C-FIND       | Optional patient/accession/date/modality/AE filters.       |
| Send DICOM          | C-STORE      | Built-in samples (CT/MR/CR/DX/US/XA) or `.dcm` / `.zip`.   |
| Print               | Print SCU    | Probes printer for medium types via N-GET, falls back to manual selection. |

## Built-in SCP

Always-on listener accepts:

- C-ECHO (Verification)
- C-FIND on Modality Worklist (replies with randomly generated dummy data
  re-rolled each launch)
- C-STORE for all standard storage SOP classes
- Basic Grayscale / Color Print Management (N-CREATE / N-SET / N-ACTION /
  N-DELETE / N-GET) — receives the print job and logs it; nothing is
  actually printed.

Configure the local port and AE title in **Settings**.

## Themes

Black, Dark and Light themes selectable in **Settings**.
