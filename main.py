#!/usr/bin/env python3
"""dicom.flux - portable DICOM tester (Echo, MWL, Store, Print + built-in SCP)."""
from __future__ import annotations

import json
import logging
import os
import random
import socket
import string
import sys
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np

import pydicom
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.uid import (
    ExplicitVRLittleEndian,
    ImplicitVRLittleEndian,
    CTImageStorage,
    MRImageStorage,
    ComputedRadiographyImageStorage,
    UltrasoundImageStorage,
    XRayAngiographicImageStorage,
    DigitalXRayImageStorageForPresentation,
    generate_uid,
)

from pynetdicom import (
    AE,
    evt,
    AllStoragePresentationContexts,
    StoragePresentationContexts,
    VerificationPresentationContexts,
    BasicWorklistManagementPresentationContexts,
)
from pynetdicom.sop_class import (
    Verification,
    ModalityWorklistInformationFind,
    BasicGrayscalePrintManagementMeta,
    BasicColorPrintManagementMeta,
    BasicFilmSession,
    BasicFilmBox,
    BasicGrayscaleImageBox,
    BasicColorImageBox,
    Printer,
)

from PySide6.QtCore import Qt, QObject, Signal, QThread, QTimer, Slot
from PySide6.QtGui import QColor, QFont, QIcon, QPixmap, QPainter, QBrush, QAction
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QTabWidget,
    QVBoxLayout,
    QHBoxLayout,
    QFormLayout,
    QGridLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QComboBox,
    QFileDialog,
    QPlainTextEdit,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QAbstractItemView,
    QGroupBox,
    QSpinBox,
    QStatusBar,
    QMessageBox,
    QSplitter,
    QFrame,
    QCheckBox,
    QStyledItemDelegate,
)

APP_NAME = "dicom.flux"
APP_VERSION = "1.0.0"
CONFIG_DIR = Path.home() / ".dicom_flux"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULT_CONFIG = {
    "local_ae": "DICOMFLUX",
    "local_port": 11112,
    "theme": "Dark",
}

MEDIUM_TYPES = [
    "PAPER",
    "CLEAR FILM",
    "BLUE FILM",
    "MAMMO CLEAR FILM",
    "MAMMO BLUE FILM",
]

MODALITIES = ["CT", "MR", "CR", "DX", "US", "XA"]


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load_config() -> dict:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return {**DEFAULT_CONFIG, **data}
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Logger - Qt-signal based
# ---------------------------------------------------------------------------
class LogBus(QObject):
    log = Signal(dict)


LOG_BUS = LogBus()


def _emit_log(direction: str, level: str, source: str, message: str, details: str = ""):
    LOG_BUS.log.emit(
        {
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "direction": direction,  # out, in, info, system
            "level": level,  # info, success, error, warning
            "source": source,
            "message": message,
            "details": details,
        }
    )


def log_out(source: str, message: str, details: str = "", level: str = "info"):
    _emit_log("out", level, source, message, details)


def log_in(source: str, message: str, details: str = "", level: str = "info"):
    _emit_log("in", level, source, message, details)


def log_info(source: str, message: str, details: str = "", level: str = "info"):
    _emit_log("info", level, source, message, details)


def log_system(source: str, message: str, details: str = "", level: str = "info"):
    _emit_log("system", level, source, message, details)


# Bridge pynetdicom's logging into our log bus so the Logs tab shows raw
# network activity at human-readable detail.
class PynetdicomLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            return
        level = "info"
        if record.levelno >= logging.ERROR:
            level = "error"
        elif record.levelno >= logging.WARNING:
            level = "warning"
        # Heuristic direction tagging based on message content.
        direction = "info"
        text = msg.lower()
        if "association request" in text or "request received" in text or "received" in text:
            direction = "in"
        elif "association acceptance" in text or "sent" in text or "sending" in text:
            direction = "out"
        _emit_log(direction, level, "pynetdicom", msg)


def install_pynetdicom_log_bridge():
    handler = PynetdicomLogHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("pynetdicom")
    logger.setLevel(logging.INFO)
    logger.handlers = [handler]
    logger.propagate = False


# ---------------------------------------------------------------------------
# Sample DICOM generation
# ---------------------------------------------------------------------------
SOP_CLASS_FOR_MODALITY = {
    "CT": CTImageStorage,
    "MR": MRImageStorage,
    "CR": ComputedRadiographyImageStorage,
    "DX": DigitalXRayImageStorageForPresentation,
    "US": UltrasoundImageStorage,
    "XA": XRayAngiographicImageStorage,
}


def _make_pattern(width: int, height: int, modality: str) -> np.ndarray:
    yy, xx = np.mgrid[0:height, 0:width]
    if modality in ("CT", "MR"):
        cx, cy = width // 2, height // 2
        r = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
        img = np.clip(2000 - r * 8, 0, 4095).astype(np.uint16)
        img += ((xx ^ yy) & 0xFF).astype(np.uint16)
    elif modality in ("CR", "DX"):
        img = (((xx // 8) + (yy // 8)) & 0x0F).astype(np.uint16) * 200
        img += np.clip((xx + yy) // 2, 0, 4095).astype(np.uint16)
    elif modality == "US":
        img = ((np.sin(xx / 12.0) * np.sin(yy / 12.0) + 1) * 127).astype(np.uint8)
        img = img.astype(np.uint16)
    else:  # XA
        img = (np.clip(np.abs(xx - width / 2) + np.abs(yy - height / 2), 0, 4095)).astype(
            np.uint16
        )
    return img


def generate_sample_dataset(modality: str) -> Dataset:
    sop_class = SOP_CLASS_FOR_MODALITY[modality]
    sop_instance = generate_uid()
    study_uid = generate_uid()
    series_uid = generate_uid()

    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = sop_class
    file_meta.MediaStorageSOPInstanceUID = sop_instance
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    file_meta.ImplementationClassUID = generate_uid()
    file_meta.ImplementationVersionName = APP_NAME

    ds = FileDataset(
        filename_or_obj="",
        dataset={},
        file_meta=file_meta,
        preamble=b"\0" * 128,
    )
    ds.is_little_endian = True
    ds.is_implicit_VR = False

    width = height = 256
    pixel = _make_pattern(width, height, modality)
    bits = 16 if pixel.dtype == np.uint16 else 8

    ds.SOPClassUID = sop_class
    ds.SOPInstanceUID = sop_instance
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.Modality = modality
    ds.PatientName = f"DICOMFLUX^SAMPLE^{modality}"
    ds.PatientID = "DF" + "".join(random.choices(string.digits, k=6))
    ds.PatientBirthDate = "19800101"
    ds.PatientSex = random.choice(["M", "F"])
    ds.AccessionNumber = "ACC" + "".join(random.choices(string.digits, k=6))
    ds.StudyID = "1"
    ds.StudyDate = datetime.now().strftime("%Y%m%d")
    ds.StudyTime = datetime.now().strftime("%H%M%S")
    ds.SeriesNumber = "1"
    ds.InstanceNumber = "1"
    ds.StudyDescription = f"dicom.flux sample {modality}"
    ds.SeriesDescription = f"sample {modality}"
    ds.Manufacturer = APP_NAME
    ds.ManufacturerModelName = APP_VERSION
    ds.ConversionType = "WSD"
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.SamplesPerPixel = 1
    ds.Rows = height
    ds.Columns = width
    ds.BitsAllocated = bits
    ds.BitsStored = bits
    ds.HighBit = bits - 1
    ds.PixelRepresentation = 0
    ds.PixelData = pixel.tobytes()
    if modality == "CT":
        ds.RescaleIntercept = "-1024"
        ds.RescaleSlope = "1"
        ds.WindowCenter = "40"
        ds.WindowWidth = "400"
    return ds


SAMPLE_DATASETS: dict[str, Dataset] = {}


def init_sample_datasets():
    for m in MODALITIES:
        SAMPLE_DATASETS[m] = generate_sample_dataset(m)


# ---------------------------------------------------------------------------
# Random MWL data (regenerated each launch)
# ---------------------------------------------------------------------------
FIRST_NAMES = [
    "JAMES", "MARY", "JOHN", "PATRICIA", "ROBERT", "JENNIFER", "MICHAEL",
    "LINDA", "DAVID", "ELIZABETH", "WILLIAM", "BARBARA", "RICHARD", "SUSAN",
    "JOSEPH", "JESSICA", "THOMAS", "SARAH", "CHARLES", "KAREN",
]
LAST_NAMES = [
    "SMITH", "JOHNSON", "BROWN", "TAYLOR", "ANDERSON", "THOMAS", "JACKSON",
    "WHITE", "HARRIS", "MARTIN", "THOMPSON", "GARCIA", "MARTINEZ", "ROBINSON",
    "CLARK", "RODRIGUEZ", "LEWIS", "LEE", "WALKER", "HALL",
]
PROCEDURE_DESCS = [
    "CHEST PA AND LATERAL", "CT HEAD WITHOUT CONTRAST", "MRI BRAIN WITH CONTRAST",
    "ABDOMEN US COMPLETE", "XR LEFT KNEE 3 VIEWS", "MAMMOGRAM SCREENING",
    "CT ABDOMEN PELVIS WITH CONTRAST", "MRI LUMBAR SPINE", "ECHOCARDIOGRAM",
    "XR CHEST PORTABLE", "DEXA SCAN", "FLUOROSCOPY UPPER GI",
]
STATIONS = ["CT01", "MR01", "MR02", "CR01", "DX01", "US01", "XA01", "MG01"]


def _random_name() -> str:
    return f"{random.choice(LAST_NAMES)}^{random.choice(FIRST_NAMES)}"


def _random_id() -> str:
    return "P" + "".join(random.choices(string.digits, k=7))


def _random_accession() -> str:
    return "A" + "".join(random.choices(string.digits, k=8))


def _random_birth() -> str:
    year = random.randint(1935, 2015)
    month = random.randint(1, 12)
    day = random.randint(1, 28)
    return f"{year:04d}{month:02d}{day:02d}"


def generate_mwl_entries(count: int = 25) -> list[dict]:
    today = datetime.now()
    entries = []
    for _ in range(count):
        delta_days = random.randint(-2, 5)
        sched_dt = today + timedelta(days=delta_days, hours=random.randint(7, 17))
        modality = random.choice(MODALITIES + ["MG"])
        entries.append(
            {
                "PatientName": _random_name(),
                "PatientID": _random_id(),
                "PatientBirthDate": _random_birth(),
                "PatientSex": random.choice(["M", "F"]),
                "AccessionNumber": _random_accession(),
                "StudyInstanceUID": generate_uid(),
                "RequestedProcedureDescription": random.choice(PROCEDURE_DESCS),
                "RequestedProcedureID": "RP" + "".join(random.choices(string.digits, k=6)),
                "ScheduledProcedureStepStartDate": sched_dt.strftime("%Y%m%d"),
                "ScheduledProcedureStepStartTime": sched_dt.strftime("%H%M%S"),
                "ScheduledStationAETitle": random.choice(STATIONS),
                "Modality": modality,
                "ScheduledProcedureStepDescription": random.choice(PROCEDURE_DESCS),
            }
        )
    return entries


MWL_ENTRIES: list[dict] = []


def init_mwl_entries():
    MWL_ENTRIES.clear()
    MWL_ENTRIES.extend(generate_mwl_entries())


# ---------------------------------------------------------------------------
# SCP server (echo, find, store, print)
# ---------------------------------------------------------------------------
class SCPServer:
    def __init__(self):
        self.ae: Optional[AE] = None
        self._server = None
        self._lock = threading.Lock()
        # Print state per-association
        self._film_sessions: dict[str, dict] = {}
        self._film_boxes: dict[str, dict] = {}

    def start(self, port: int, ae_title: str):
        self.stop()
        ae = AE(ae_title=ae_title)
        # Accept verification, MWL, all storage classes, and print management.
        ae.add_supported_context(Verification)
        ae.add_supported_context(ModalityWorklistInformationFind)
        for ctx in AllStoragePresentationContexts:
            ae.add_supported_context(ctx.abstract_syntax)
        # Print management - need both meta and component SOP classes.
        ae.add_supported_context(BasicGrayscalePrintManagementMeta)
        ae.add_supported_context(BasicColorPrintManagementMeta)
        ae.add_supported_context(BasicFilmSession)
        ae.add_supported_context(BasicFilmBox)
        ae.add_supported_context(BasicGrayscaleImageBox)
        ae.add_supported_context(BasicColorImageBox)
        ae.add_supported_context(Printer)

        handlers = [
            (evt.EVT_C_ECHO, self._on_c_echo),
            (evt.EVT_C_FIND, self._on_c_find),
            (evt.EVT_C_STORE, self._on_c_store),
            (evt.EVT_N_CREATE, self._on_n_create),
            (evt.EVT_N_SET, self._on_n_set),
            (evt.EVT_N_ACTION, self._on_n_action),
            (evt.EVT_N_DELETE, self._on_n_delete),
            (evt.EVT_N_GET, self._on_n_get),
            (evt.EVT_CONN_OPEN, self._on_conn_open),
            (evt.EVT_CONN_CLOSE, self._on_conn_close),
            (evt.EVT_REQUESTED, self._on_requested),
            (evt.EVT_RELEASED, self._on_released),
            (evt.EVT_ABORTED, self._on_aborted),
        ]

        try:
            self._server = ae.start_server(
                ("0.0.0.0", port), block=False, evt_handlers=handlers
            )
            self.ae = ae
            log_system("SCP", f"Listening on port {port} as AE '{ae_title}'", level="success")
        except Exception as exc:
            log_system("SCP", f"Failed to start listener on port {port}", str(exc), level="error")

    def stop(self):
        with self._lock:
            if self._server is not None:
                try:
                    self._server.shutdown()
                except Exception:
                    pass
                self._server = None
                self.ae = None
                log_system("SCP", "Listener stopped")

    # ----- event handlers ------------------------------------------------
    def _on_conn_open(self, event):
        peer = event.address
        log_in("SCP", f"TCP connection opened from {peer[0]}:{peer[1]}")

    def _on_conn_close(self, event):
        peer = event.address
        log_in("SCP", f"TCP connection closed from {peer[0]}:{peer[1]}")

    def _on_requested(self, event):
        ar = event.assoc.requestor
        log_in(
            "SCP",
            f"Association request from {ar.ae_title} ({ar.address}:{ar.port})",
            f"Calling AE: {ar.ae_title}\nCalled AE:  {event.assoc.acceptor.ae_title}",
        )

    def _on_released(self, event):
        ar = event.assoc.requestor
        log_in("SCP", f"Association released by {ar.ae_title}", level="info")

    def _on_aborted(self, event):
        log_in("SCP", "Association aborted", level="warning")

    def _on_c_echo(self, event):
        ar = event.assoc.requestor
        log_in("SCP", f"C-ECHO received from {ar.ae_title}", level="success")
        return 0x0000

    def _on_c_find(self, event):
        ar = event.assoc.requestor
        req_ds: Dataset = event.identifier
        if event.request.AffectedSOPClassUID != ModalityWorklistInformationFind:
            log_in("SCP", f"C-FIND on unsupported SOP class", str(event.request.AffectedSOPClassUID), level="warning")
            yield 0xC000, None
            return

        log_in(
            "SCP",
            f"C-FIND (MWL) request from {ar.ae_title}",
            self._format_dataset(req_ds),
        )

        for entry in self._match_mwl(req_ds):
            if event.is_cancelled:
                yield 0xFE00, None
                return
            ds = self._build_mwl_response(entry)
            log_out("SCP", f"C-FIND response for {ds.PatientName} / {ds.AccessionNumber}", level="info")
            yield 0xFF00, ds
        yield 0x0000, None

    def _on_c_store(self, event):
        ar = event.assoc.requestor
        ds: Dataset = event.dataset
        ds.file_meta = event.file_meta
        info = (
            f"Patient:  {getattr(ds, 'PatientName', '?')}\n"
            f"PatientID: {getattr(ds, 'PatientID', '?')}\n"
            f"Modality: {getattr(ds, 'Modality', '?')}\n"
            f"SOP Class: {ds.file_meta.MediaStorageSOPClassUID}\n"
            f"SOP Instance: {ds.file_meta.MediaStorageSOPInstanceUID}\n"
            f"Transfer Syntax: {ds.file_meta.TransferSyntaxUID}"
        )
        log_in("SCP", f"C-STORE received from {ar.ae_title}", info, level="success")
        return 0x0000

    # --- print handlers --------------------------------------------------
    def _on_n_create(self, event):
        sop_class = event.request.AffectedSOPClassUID
        sop_instance = event.request.AffectedSOPInstanceUID or generate_uid()
        attrs = event.attribute_list or Dataset()
        if sop_class == BasicFilmSession:
            self._film_sessions[sop_instance] = {"attrs": attrs}
            log_in("SCP/Print", f"N-CREATE Basic Film Session", self._format_dataset(attrs), level="success")
        elif sop_class == BasicFilmBox:
            self._film_boxes[sop_instance] = {"attrs": attrs}
            # Create child image box references in response
            image_box_uid = generate_uid()
            rsp = Dataset()
            rsp.update(attrs)
            ib_ref = Dataset()
            ib_ref.ReferencedSOPClassUID = BasicGrayscaleImageBox
            ib_ref.ReferencedSOPInstanceUID = image_box_uid
            rsp.ReferencedImageBoxSequence = [ib_ref]
            log_in("SCP/Print", "N-CREATE Basic Film Box", self._format_dataset(attrs), level="success")
            return 0x0000, rsp
        else:
            log_in("SCP/Print", f"N-CREATE on {sop_class}", level="info")
        return 0x0000, attrs

    def _on_n_set(self, event):
        sop_class = event.request.RequestedSOPClassUID
        attrs = event.attribute_list or Dataset()
        details = self._format_dataset(attrs, max_keys=20)
        if sop_class in (BasicGrayscaleImageBox, BasicColorImageBox):
            log_in("SCP/Print", "N-SET Image Box (image data received)", details, level="success")
        else:
            log_in("SCP/Print", f"N-SET on {sop_class}", details)
        return 0x0000, attrs

    def _on_n_action(self, event):
        sop_class = event.request.RequestedSOPClassUID
        action_id = event.action_type
        log_in(
            "SCP/Print",
            f"N-ACTION (print) on {sop_class} action {action_id}",
            "Print job accepted by virtual printer.",
            level="success",
        )
        return 0x0000, None

    def _on_n_delete(self, event):
        sop_class = event.request.RequestedSOPClassUID
        sop_instance = event.request.RequestedSOPInstanceUID
        self._film_sessions.pop(sop_instance, None)
        self._film_boxes.pop(sop_instance, None)
        log_in("SCP/Print", f"N-DELETE {sop_class}")
        return 0x0000

    def _on_n_get(self, event):
        sop_class = event.request.RequestedSOPClassUID
        if sop_class == Printer:
            ds = Dataset()
            ds.PrinterStatus = "NORMAL"
            ds.PrinterStatusInfo = "NORMAL"
            ds.PrinterName = APP_NAME
            ds.Manufacturer = APP_NAME
            ds.ManufacturerModelName = APP_VERSION
            log_in("SCP/Print", "N-GET Printer status query", "Returning NORMAL", level="success")
            return 0x0000, ds
        log_in("SCP/Print", f"N-GET on {sop_class}")
        return 0x0000, Dataset()

    # ----- helpers -------------------------------------------------------
    @staticmethod
    def _format_dataset(ds: Dataset, max_keys: int = 30) -> str:
        if not ds:
            return ""
        lines = []
        for i, elem in enumerate(ds.iterall()):
            if i >= max_keys:
                lines.append("  ...")
                break
            try:
                lines.append(f"  {elem.keyword or elem.tag}: {elem.value!r}")
            except Exception:
                lines.append(f"  {elem.tag}: <unreadable>")
        return "\n".join(lines)

    @staticmethod
    def _match_mwl(req: Dataset) -> list[dict]:
        out = []
        pn_q = str(getattr(req, "PatientName", "") or "").upper().replace("*", "")
        pid_q = str(getattr(req, "PatientID", "") or "").replace("*", "")
        acc_q = str(getattr(req, "AccessionNumber", "") or "").replace("*", "")
        sps_q = (
            req.ScheduledProcedureStepSequence[0]
            if "ScheduledProcedureStepSequence" in req and req.ScheduledProcedureStepSequence
            else Dataset()
        )
        date_q = str(getattr(sps_q, "ScheduledProcedureStepStartDate", "") or "")
        mod_q = str(getattr(sps_q, "Modality", "") or "")
        ae_q = str(getattr(sps_q, "ScheduledStationAETitle", "") or "")

        for e in MWL_ENTRIES:
            if pn_q and pn_q not in e["PatientName"]:
                continue
            if pid_q and pid_q not in e["PatientID"]:
                continue
            if acc_q and acc_q not in e["AccessionNumber"]:
                continue
            if mod_q and mod_q != e["Modality"]:
                continue
            if ae_q and ae_q != e["ScheduledStationAETitle"]:
                continue
            if date_q:
                # support YYYYMMDD or YYYYMMDD-YYYYMMDD
                if "-" in date_q:
                    a, b = date_q.split("-", 1)
                    if not (a <= e["ScheduledProcedureStepStartDate"] <= b):
                        continue
                elif date_q != e["ScheduledProcedureStepStartDate"]:
                    continue
            out.append(e)
        return out

    @staticmethod
    def _build_mwl_response(entry: dict) -> Dataset:
        ds = Dataset()
        ds.PatientName = entry["PatientName"]
        ds.PatientID = entry["PatientID"]
        ds.PatientBirthDate = entry["PatientBirthDate"]
        ds.PatientSex = entry["PatientSex"]
        ds.AccessionNumber = entry["AccessionNumber"]
        ds.StudyInstanceUID = entry["StudyInstanceUID"]
        ds.RequestedProcedureDescription = entry["RequestedProcedureDescription"]
        ds.RequestedProcedureID = entry["RequestedProcedureID"]
        sps = Dataset()
        sps.ScheduledProcedureStepStartDate = entry["ScheduledProcedureStepStartDate"]
        sps.ScheduledProcedureStepStartTime = entry["ScheduledProcedureStepStartTime"]
        sps.ScheduledStationAETitle = entry["ScheduledStationAETitle"]
        sps.Modality = entry["Modality"]
        sps.ScheduledProcedureStepDescription = entry["ScheduledProcedureStepDescription"]
        ds.ScheduledProcedureStepSequence = [sps]
        return ds


SCP = SCPServer()


# ---------------------------------------------------------------------------
# SCU operations - run in worker threads
# ---------------------------------------------------------------------------
class Worker(QThread):
    finished_with_result = Signal(bool, str, str)  # ok, summary, details

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs

    def run(self):
        try:
            ok, summary, details = self.fn(*self.args, **self.kwargs)
        except Exception as exc:
            ok, summary, details = False, f"Exception: {exc}", repr(exc)
        self.finished_with_result.emit(ok, summary, details)


def scu_echo(local_ae: str, ip: str, port: int, remote_ae: str, timeout: int = 10):
    log_out("Echo SCU", f"Associating with {remote_ae}@{ip}:{port}")
    ae = AE(ae_title=local_ae)
    ae.add_requested_context(Verification)
    ae.acse_timeout = timeout
    ae.dimse_timeout = timeout
    ae.network_timeout = timeout
    t0 = time.time()
    assoc = ae.associate(ip, port, ae_title=remote_ae)
    if not assoc.is_established:
        return False, "Association rejected or timed out", ""
    try:
        status = assoc.send_c_echo()
        elapsed_ms = (time.time() - t0) * 1000
        if status and getattr(status, "Status", None) == 0x0000:
            log_in("Echo SCU", f"C-ECHO success in {elapsed_ms:.0f} ms", level="success")
            return True, f"Echo successful ({elapsed_ms:.0f} ms)", f"Status: 0x{status.Status:04X}"
        else:
            return False, "C-ECHO failed", f"Status: {status}"
    finally:
        assoc.release()


def scu_find_mwl(
    local_ae: str,
    ip: str,
    port: int,
    remote_ae: str,
    filters: dict,
    timeout: int = 30,
):
    log_out("MWL SCU", f"Querying {remote_ae}@{ip}:{port}", json.dumps(filters, indent=2))
    ae = AE(ae_title=local_ae)
    ae.add_requested_context(ModalityWorklistInformationFind)
    ae.acse_timeout = timeout
    ae.dimse_timeout = timeout
    ae.network_timeout = timeout

    ds = Dataset()
    ds.PatientName = filters.get("PatientName", "")
    ds.PatientID = filters.get("PatientID", "")
    ds.PatientBirthDate = ""
    ds.PatientSex = ""
    ds.AccessionNumber = filters.get("AccessionNumber", "")
    ds.StudyInstanceUID = ""
    ds.RequestedProcedureDescription = ""
    ds.RequestedProcedureID = ""
    sps = Dataset()
    sps.ScheduledProcedureStepStartDate = filters.get("ScheduledProcedureStepStartDate", "")
    sps.ScheduledProcedureStepStartTime = ""
    sps.ScheduledStationAETitle = filters.get("ScheduledStationAETitle", "")
    sps.Modality = filters.get("Modality", "")
    sps.ScheduledProcedureStepDescription = ""
    ds.ScheduledProcedureStepSequence = [sps]

    assoc = ae.associate(ip, port, ae_title=remote_ae)
    if not assoc.is_established:
        return False, "Association rejected or timed out", ""

    rows: list[dict] = []
    try:
        responses = assoc.send_c_find(ds, ModalityWorklistInformationFind)
        for status, identifier in responses:
            if status and getattr(status, "Status", None) in (0xFF00, 0xFF01) and identifier:
                row = {
                    "PatientName": str(getattr(identifier, "PatientName", "")),
                    "PatientID": str(getattr(identifier, "PatientID", "")),
                    "AccessionNumber": str(getattr(identifier, "AccessionNumber", "")),
                }
                if "ScheduledProcedureStepSequence" in identifier and identifier.ScheduledProcedureStepSequence:
                    s = identifier.ScheduledProcedureStepSequence[0]
                    row["Date"] = str(getattr(s, "ScheduledProcedureStepStartDate", ""))
                    row["Time"] = str(getattr(s, "ScheduledProcedureStepStartTime", ""))
                    row["Modality"] = str(getattr(s, "Modality", ""))
                    row["Station AE"] = str(getattr(s, "ScheduledStationAETitle", ""))
                    row["Procedure"] = str(getattr(s, "ScheduledProcedureStepDescription", ""))
                rows.append(row)
                log_in("MWL SCU", f"Match: {row['PatientName']} / {row.get('Date', '')}")
    finally:
        assoc.release()

    summary = f"Received {len(rows)} matching worklist entries"
    return True, summary, json.dumps(rows, indent=2)


def scu_store(
    local_ae: str,
    ip: str,
    port: int,
    remote_ae: str,
    datasets: list[Dataset],
    timeout: int = 30,
):
    log_out("Store SCU", f"Sending {len(datasets)} object(s) to {remote_ae}@{ip}:{port}")
    ae = AE(ae_title=local_ae)
    # Add presentation contexts for each unique SOP class.
    for ds in datasets:
        sop = (
            getattr(ds.file_meta, "MediaStorageSOPClassUID", None)
            if hasattr(ds, "file_meta")
            else None
        ) or getattr(ds, "SOPClassUID", None)
        ts = (
            getattr(ds.file_meta, "TransferSyntaxUID", None)
            if hasattr(ds, "file_meta")
            else None
        ) or ExplicitVRLittleEndian
        if sop:
            ae.add_requested_context(sop, [ts, ImplicitVRLittleEndian, ExplicitVRLittleEndian])
    ae.acse_timeout = timeout
    ae.dimse_timeout = timeout
    ae.network_timeout = timeout

    assoc = ae.associate(ip, port, ae_title=remote_ae)
    if not assoc.is_established:
        return False, "Association rejected or timed out", ""

    sent_ok = 0
    failures: list[str] = []
    try:
        for i, ds in enumerate(datasets, 1):
            status = assoc.send_c_store(ds)
            if status and getattr(status, "Status", None) == 0x0000:
                sent_ok += 1
                log_in(
                    "Store SCU",
                    f"C-STORE {i}/{len(datasets)} success",
                    f"SOP Instance: {getattr(ds, 'SOPInstanceUID', '?')}",
                    level="success",
                )
            else:
                msg = f"C-STORE {i}/{len(datasets)} failed: status {status}"
                log_in("Store SCU", msg, level="error")
                failures.append(msg)
    finally:
        assoc.release()

    ok = sent_ok == len(datasets)
    summary = f"Sent {sent_ok}/{len(datasets)} objects"
    return ok, summary, "\n".join(failures) if failures else ""


def scu_query_printer(
    local_ae: str, ip: str, port: int, remote_ae: str, timeout: int = 10
):
    log_out("Print SCU", f"Querying printer {remote_ae}@{ip}:{port}")
    ae = AE(ae_title=local_ae)
    ae.add_requested_context(BasicGrayscalePrintManagementMeta)
    ae.acse_timeout = timeout
    ae.dimse_timeout = timeout
    ae.network_timeout = timeout
    assoc = ae.associate(ip, port, ae_title=remote_ae)
    if not assoc.is_established:
        return False, "Association rejected or timed out", ""
    try:
        status, attrs = assoc.send_n_get(
            [
                0x21100010,  # PrinterStatus
                0x21100020,  # PrinterStatusInfo
                0x21100030,  # PrinterName
                0x00080070,  # Manufacturer
                0x00081090,  # ManufacturerModelName
            ],
            Printer,
            "1.2.840.10008.5.1.1.17",  # well-known Printer SOP Instance UID
            meta_uid=BasicGrayscalePrintManagementMeta,
        )
        if status and getattr(status, "Status", None) == 0x0000:
            details = SCPServer._format_dataset(attrs) if attrs else ""
            log_in("Print SCU", "Printer status NORMAL", details, level="success")
            return True, "Printer reachable. Select a medium type below.", details
        else:
            return False, f"Printer query failed (status {status})", ""
    finally:
        assoc.release()


def scu_print(
    local_ae: str,
    ip: str,
    port: int,
    remote_ae: str,
    dataset: Dataset,
    medium_type: str,
    timeout: int = 30,
):
    log_out(
        "Print SCU",
        f"Print job to {remote_ae}@{ip}:{port}",
        f"Medium: {medium_type}",
    )
    ae = AE(ae_title=local_ae)
    ae.add_requested_context(BasicGrayscalePrintManagementMeta)
    ae.acse_timeout = timeout
    ae.dimse_timeout = timeout
    ae.network_timeout = timeout

    assoc = ae.associate(ip, port, ae_title=remote_ae)
    if not assoc.is_established:
        return False, "Association rejected or timed out", ""

    try:
        # 1. N-CREATE Basic Film Session
        film_session_uid = generate_uid()
        fs_attrs = Dataset()
        fs_attrs.NumberOfCopies = "1"
        fs_attrs.PrintPriority = "MED"
        fs_attrs.MediumType = medium_type
        fs_attrs.FilmDestination = "MAGAZINE"
        fs_attrs.FilmSessionLabel = "DICOMFLUX"
        status, fs_rsp = assoc.send_n_create(
            fs_attrs,
            BasicFilmSession,
            film_session_uid,
            meta_uid=BasicGrayscalePrintManagementMeta,
        )
        if not status or status.Status != 0x0000:
            return False, "N-CREATE Film Session failed", f"Status: {status}"
        log_in("Print SCU", "Film Session created", level="success")

        # 2. N-CREATE Basic Film Box
        film_box_uid = generate_uid()
        fb_attrs = Dataset()
        fb_attrs.ImageDisplayFormat = "STANDARD\\1,1"
        fb_attrs.FilmOrientation = "PORTRAIT"
        fb_attrs.FilmSizeID = "8INX10IN"
        fb_attrs.MagnificationType = "REPLICATE"
        fb_attrs.SmoothingType = "MEDIUM"
        fb_attrs.BorderDensity = "BLACK"
        fb_attrs.EmptyImageDensity = "BLACK"
        fb_attrs.Trim = "NO"
        fs_ref = Dataset()
        fs_ref.ReferencedSOPClassUID = BasicFilmSession
        fs_ref.ReferencedSOPInstanceUID = film_session_uid
        fb_attrs.ReferencedFilmSessionSequence = [fs_ref]
        status, fb_rsp = assoc.send_n_create(
            fb_attrs,
            BasicFilmBox,
            film_box_uid,
            meta_uid=BasicGrayscalePrintManagementMeta,
        )
        if not status or status.Status != 0x0000:
            return False, "N-CREATE Film Box failed", f"Status: {status}"
        log_in("Print SCU", "Film Box created", level="success")

        # Extract referenced image box UID from the SCP response.
        image_box_uid = None
        if fb_rsp and "ReferencedImageBoxSequence" in fb_rsp and fb_rsp.ReferencedImageBoxSequence:
            image_box_uid = fb_rsp.ReferencedImageBoxSequence[0].ReferencedSOPInstanceUID
        if not image_box_uid:
            return False, "SCP did not return an Image Box reference", ""

        # 3. N-SET Basic Grayscale Image Box
        # Build a "preformatted grayscale image" dataset from the source image.
        if "PixelData" not in dataset:
            return False, "Selected dataset has no pixel data to print", ""

        img_seq = Dataset()
        img_seq.SamplesPerPixel = getattr(dataset, "SamplesPerPixel", 1)
        img_seq.PhotometricInterpretation = getattr(
            dataset, "PhotometricInterpretation", "MONOCHROME2"
        )
        img_seq.Rows = dataset.Rows
        img_seq.Columns = dataset.Columns
        img_seq.BitsAllocated = dataset.BitsAllocated
        img_seq.BitsStored = dataset.BitsStored
        img_seq.HighBit = dataset.HighBit
        img_seq.PixelRepresentation = getattr(dataset, "PixelRepresentation", 0)
        img_seq.PixelData = dataset.PixelData

        ib_attrs = Dataset()
        ib_attrs.ImagePosition = 1
        ib_attrs.Polarity = "NORMAL"
        ib_attrs.MagnificationType = "REPLICATE"
        ib_attrs.SmoothingType = "MEDIUM"
        ib_attrs.PreformattedGrayscaleImageSequence = [img_seq]
        status, _ = assoc.send_n_set(
            ib_attrs,
            BasicGrayscaleImageBox,
            image_box_uid,
            meta_uid=BasicGrayscalePrintManagementMeta,
        )
        if not status or status.Status != 0x0000:
            return False, "N-SET Image Box failed", f"Status: {status}"
        log_in("Print SCU", "Image Box set with pixel data", level="success")

        # 4. N-ACTION on Film Box (action 1 = print)
        status, _ = assoc.send_n_action(
            None,
            1,
            BasicFilmBox,
            film_box_uid,
            meta_uid=BasicGrayscalePrintManagementMeta,
        )
        if not status or status.Status != 0x0000:
            return False, "N-ACTION print failed", f"Status: {status}"
        log_in("Print SCU", "Print action accepted", level="success")

        # 5. Cleanup
        try:
            assoc.send_n_delete(
                BasicFilmBox,
                film_box_uid,
                meta_uid=BasicGrayscalePrintManagementMeta,
            )
            assoc.send_n_delete(
                BasicFilmSession,
                film_session_uid,
                meta_uid=BasicGrayscalePrintManagementMeta,
            )
        except Exception:
            pass

        return True, "Print job sent successfully", ""
    finally:
        assoc.release()


# ---------------------------------------------------------------------------
# Themes
# ---------------------------------------------------------------------------
THEMES = {
    "Black": {
        "bg": "#000000",
        "panel": "#0a0a0a",
        "fg": "#e6e6e6",
        "muted": "#888",
        "accent": "#3aa0ff",
        "border": "#252525",
        "hover": "#1a1a1a",
        "selected": "#1f2c3a",
        "out": "#3aa0ff",
        "in": "#b85cff",
        "ok": "#22c55e",
        "err": "#ef4444",
        "warn": "#f59e0b",
    },
    "Dark": {
        "bg": "#1f2126",
        "panel": "#2a2d33",
        "fg": "#e6e6e6",
        "muted": "#9aa0a6",
        "accent": "#5aa9ff",
        "border": "#3a3f47",
        "hover": "#33373f",
        "selected": "#324a67",
        "out": "#5aa9ff",
        "in": "#c084fc",
        "ok": "#22c55e",
        "err": "#ef4444",
        "warn": "#f59e0b",
    },
    "Light": {
        "bg": "#f4f5f7",
        "panel": "#ffffff",
        "fg": "#1f2937",
        "muted": "#6b7280",
        "accent": "#1e6fea",
        "border": "#d1d5db",
        "hover": "#e5e7eb",
        "selected": "#cfe1ff",
        "out": "#1e6fea",
        "in": "#7c3aed",
        "ok": "#15803d",
        "err": "#b91c1c",
        "warn": "#b45309",
    },
}


def build_qss(theme: dict) -> str:
    return f"""
    QWidget {{
        background-color: {theme['bg']};
        color: {theme['fg']};
        font-size: 13px;
    }}
    QGroupBox {{
        border: 1px solid {theme['border']};
        margin-top: 14px;
        padding-top: 10px;
        border-radius: 6px;
        background-color: {theme['panel']};
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: 10px;
        padding: 0 6px;
        color: {theme['muted']};
    }}
    QLineEdit, QComboBox, QSpinBox, QPlainTextEdit, QTextEdit {{
        background-color: {theme['panel']};
        border: 1px solid {theme['border']};
        padding: 6px 8px;
        border-radius: 4px;
        selection-background-color: {theme['selected']};
        color: {theme['fg']};
    }}
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{
        border: 1px solid {theme['accent']};
    }}
    QPushButton {{
        background-color: {theme['hover']};
        border: 1px solid {theme['border']};
        padding: 7px 16px;
        border-radius: 4px;
        color: {theme['fg']};
    }}
    QPushButton:hover {{
        background-color: {theme['selected']};
        border: 1px solid {theme['accent']};
    }}
    QPushButton:pressed {{
        background-color: {theme['border']};
    }}
    QPushButton:disabled {{
        color: {theme['muted']};
    }}
    QPushButton#primary {{
        background-color: {theme['accent']};
        color: #ffffff;
        border: 1px solid {theme['accent']};
    }}
    QPushButton#primary:hover {{
        background-color: {theme['accent']};
        border: 1px solid {theme['fg']};
    }}
    QTabWidget::pane {{
        border: 1px solid {theme['border']};
        background-color: {theme['bg']};
        top: -1px;
    }}
    QTabBar::tab {{
        background: {theme['panel']};
        color: {theme['muted']};
        padding: 9px 18px;
        border: 1px solid {theme['border']};
        border-bottom: none;
        border-top-left-radius: 4px;
        border-top-right-radius: 4px;
        margin-right: 2px;
    }}
    QTabBar::tab:selected {{
        background: {theme['bg']};
        color: {theme['fg']};
        border-bottom: 1px solid {theme['bg']};
    }}
    QTableWidget {{
        background-color: {theme['panel']};
        gridline-color: {theme['border']};
        selection-background-color: {theme['selected']};
        selection-color: {theme['fg']};
        alternate-background-color: {theme['hover']};
    }}
    QHeaderView::section {{
        background-color: {theme['hover']};
        color: {theme['fg']};
        padding: 6px;
        border: 1px solid {theme['border']};
    }}
    QStatusBar {{
        background-color: {theme['panel']};
        color: {theme['muted']};
        border-top: 1px solid {theme['border']};
    }}
    QLabel#title {{
        font-size: 16px;
        font-weight: 600;
    }}
    QLabel#muted {{
        color: {theme['muted']};
    }}
    QLabel#status-ok {{
        color: {theme['ok']};
        font-weight: 600;
    }}
    QLabel#status-err {{
        color: {theme['err']};
        font-weight: 600;
    }}
    QLabel#status-info {{
        color: {theme['accent']};
    }}
    QFrame#card {{
        background-color: {theme['panel']};
        border: 1px solid {theme['border']};
        border-radius: 6px;
    }}
    QScrollBar:vertical {{
        background: {theme['bg']};
        width: 10px;
    }}
    QScrollBar::handle:vertical {{
        background: {theme['border']};
        min-height: 20px;
        border-radius: 5px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {theme['muted']};
    }}
    QScrollBar:horizontal {{
        background: {theme['bg']};
        height: 10px;
    }}
    QScrollBar::handle:horizontal {{
        background: {theme['border']};
        min-width: 20px;
        border-radius: 5px;
    }}
    QSplitter::handle {{
        background: {theme['border']};
    }}
    """


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------
def make_destination_box(default_port: int = 104) -> tuple[QGroupBox, QLineEdit, QSpinBox, QLineEdit]:
    box = QGroupBox("Destination")
    grid = QGridLayout(box)
    ip = QLineEdit()
    ip.setPlaceholderText("127.0.0.1")
    port = QSpinBox()
    port.setRange(1, 65535)
    port.setValue(default_port)
    ae = QLineEdit()
    ae.setPlaceholderText("REMOTE_AE")
    grid.addWidget(QLabel("IP Address"), 0, 0)
    grid.addWidget(ip, 0, 1)
    grid.addWidget(QLabel("Port"), 0, 2)
    grid.addWidget(port, 0, 3)
    grid.addWidget(QLabel("AE Title"), 0, 4)
    grid.addWidget(ae, 0, 5)
    grid.setColumnStretch(1, 2)
    grid.setColumnStretch(5, 2)
    return box, ip, port, ae


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
class EchoTab(QWidget):
    def __init__(self, get_local):
        super().__init__()
        self.get_local = get_local
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        title = QLabel("DICOM C-ECHO")
        title.setObjectName("title")
        subtitle = QLabel("Verify connectivity and AE title acceptance with a remote DICOM node.")
        subtitle.setObjectName("muted")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        dest, self.ip, self.port, self.ae = make_destination_box(104)
        layout.addWidget(dest)

        btn_row = QHBoxLayout()
        self.send_btn = QPushButton("Send Echo")
        self.send_btn.setObjectName("primary")
        self.send_btn.clicked.connect(self.do_echo)
        btn_row.addWidget(self.send_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        result_group = QGroupBox("Result")
        rg_layout = QVBoxLayout(result_group)
        self.status_label = QLabel("Idle")
        self.status_label.setObjectName("muted")
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumBlockCount(500)
        rg_layout.addWidget(self.status_label)
        rg_layout.addWidget(self.details, 1)
        layout.addWidget(result_group, 1)

    def do_echo(self):
        ip = self.ip.text().strip() or "127.0.0.1"
        port = self.port.value()
        ae = self.ae.text().strip() or "ANY-SCP"
        local_ae = self.get_local()["local_ae"]
        self.send_btn.setEnabled(False)
        self.status_label.setText(f"Sending C-ECHO to {ae}@{ip}:{port}...")
        self.status_label.setObjectName("status-info")
        self.status_label.style().polish(self.status_label)

        worker = Worker(scu_echo, local_ae, ip, port, ae)
        worker.finished_with_result.connect(self._done)
        worker.finished.connect(worker.deleteLater)
        self._worker = worker
        worker.start()

    def _done(self, ok: bool, summary: str, details: str):
        self.send_btn.setEnabled(True)
        self.status_label.setText(("OK   " if ok else "FAIL ") + summary)
        self.status_label.setObjectName("status-ok" if ok else "status-err")
        self.status_label.style().polish(self.status_label)
        self.details.setPlainText(details)


class MWLTab(QWidget):
    def __init__(self, get_local):
        super().__init__()
        self.get_local = get_local
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        title = QLabel("Modality Worklist (C-FIND)")
        title.setObjectName("title")
        subtitle = QLabel("Leave fields blank to fetch all entries. Date supports YYYYMMDD or YYYYMMDD-YYYYMMDD.")
        subtitle.setObjectName("muted")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        dest, self.ip, self.port, self.ae = make_destination_box(104)
        layout.addWidget(dest)

        filter_box = QGroupBox("Filters (all optional)")
        grid = QGridLayout(filter_box)
        self.f_pn = QLineEdit()
        self.f_pid = QLineEdit()
        self.f_acc = QLineEdit()
        self.f_date = QLineEdit()
        self.f_mod = QLineEdit()
        self.f_station = QLineEdit()
        self.f_pn.setPlaceholderText("e.g. SMITH^*")
        self.f_date.setPlaceholderText("YYYYMMDD or YYYYMMDD-YYYYMMDD")
        grid.addWidget(QLabel("Patient Name"), 0, 0); grid.addWidget(self.f_pn, 0, 1)
        grid.addWidget(QLabel("Patient ID"), 0, 2);  grid.addWidget(self.f_pid, 0, 3)
        grid.addWidget(QLabel("Accession #"), 1, 0); grid.addWidget(self.f_acc, 1, 1)
        grid.addWidget(QLabel("Sched. Date"), 1, 2); grid.addWidget(self.f_date, 1, 3)
        grid.addWidget(QLabel("Modality"), 2, 0);    grid.addWidget(self.f_mod, 2, 1)
        grid.addWidget(QLabel("Sched. Station AE"), 2, 2); grid.addWidget(self.f_station, 2, 3)
        layout.addWidget(filter_box)

        btn_row = QHBoxLayout()
        self.send_btn = QPushButton("Run Worklist Query")
        self.send_btn.setObjectName("primary")
        self.send_btn.clicked.connect(self.do_query)
        self.status_label = QLabel("Idle")
        self.status_label.setObjectName("muted")
        btn_row.addWidget(self.send_btn)
        btn_row.addWidget(self.status_label)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["Patient Name", "Patient ID", "Accession #", "Date", "Time", "Modality", "Procedure"]
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        layout.addWidget(self.table, 1)

    def do_query(self):
        ip = self.ip.text().strip() or "127.0.0.1"
        port = self.port.value()
        ae = self.ae.text().strip() or "ANY-SCP"
        local_ae = self.get_local()["local_ae"]
        filters = {
            "PatientName": self.f_pn.text().strip(),
            "PatientID": self.f_pid.text().strip(),
            "AccessionNumber": self.f_acc.text().strip(),
            "ScheduledProcedureStepStartDate": self.f_date.text().strip(),
            "Modality": self.f_mod.text().strip().upper(),
            "ScheduledStationAETitle": self.f_station.text().strip().upper(),
        }
        self.send_btn.setEnabled(False)
        self.status_label.setText("Querying...")
        self.status_label.setObjectName("status-info")
        self.status_label.style().polish(self.status_label)

        worker = Worker(scu_find_mwl, local_ae, ip, port, ae, filters)
        worker.finished_with_result.connect(self._done)
        worker.finished.connect(worker.deleteLater)
        self._worker = worker
        worker.start()

    def _done(self, ok: bool, summary: str, details: str):
        self.send_btn.setEnabled(True)
        self.status_label.setText(summary)
        self.status_label.setObjectName("status-ok" if ok else "status-err")
        self.status_label.style().polish(self.status_label)
        self.table.setRowCount(0)
        if ok and details:
            try:
                rows = json.loads(details)
            except Exception:
                rows = []
            for row in rows:
                r = self.table.rowCount()
                self.table.insertRow(r)
                for c, key in enumerate([
                    "PatientName", "PatientID", "AccessionNumber",
                    "Date", "Time", "Modality", "Procedure",
                ]):
                    self.table.setItem(r, c, QTableWidgetItem(str(row.get(key, ""))))


class SendTab(QWidget):
    def __init__(self, get_local):
        super().__init__()
        self.get_local = get_local
        self._uploaded: list[Dataset] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        title = QLabel("Send DICOM (C-STORE)")
        title.setObjectName("title")
        subtitle = QLabel("Pick a built-in sample modality or upload your own .dcm or .zip of .dcm files.")
        subtitle.setObjectName("muted")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        dest, self.ip, self.port, self.ae = make_destination_box(104)
        layout.addWidget(dest)

        src_box = QGroupBox("Source")
        sl = QGridLayout(src_box)
        sl.addWidget(QLabel("Built-in sample"), 0, 0)
        self.modality_combo = QComboBox()
        self.modality_combo.addItems(MODALITIES)
        sl.addWidget(self.modality_combo, 0, 1)
        self.upload_btn = QPushButton("Upload .dcm or .zip...")
        self.upload_btn.clicked.connect(self.pick_files)
        sl.addWidget(self.upload_btn, 0, 2)
        self.upload_label = QLabel("(no files uploaded - using sample)")
        self.upload_label.setObjectName("muted")
        sl.addWidget(self.upload_label, 1, 0, 1, 3)
        sl.setColumnStretch(1, 1)
        layout.addWidget(src_box)

        btn_row = QHBoxLayout()
        self.send_btn = QPushButton("Send")
        self.send_btn.setObjectName("primary")
        self.send_btn.clicked.connect(self.do_send)
        self.status_label = QLabel("Idle")
        self.status_label.setObjectName("muted")
        btn_row.addWidget(self.send_btn)
        btn_row.addWidget(self.status_label)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        result_group = QGroupBox("Result")
        rg_layout = QVBoxLayout(result_group)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        rg_layout.addWidget(self.details)
        layout.addWidget(result_group, 1)

    def pick_files(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select DICOM file or ZIP", "", "DICOM/ZIP (*.dcm *.zip);;All Files (*)"
        )
        if not path:
            return
        self._uploaded = self._load_path(path)
        if self._uploaded:
            self.upload_label.setText(f"Uploaded {len(self._uploaded)} dataset(s) from {Path(path).name}")
            self.upload_label.setObjectName("status-ok")
        else:
            self.upload_label.setText(f"Could not load any DICOM files from {Path(path).name}")
            self.upload_label.setObjectName("status-err")
        self.upload_label.style().polish(self.upload_label)

    @staticmethod
    def _load_path(path: str) -> list[Dataset]:
        p = Path(path)
        out: list[Dataset] = []
        if p.suffix.lower() == ".zip":
            try:
                with zipfile.ZipFile(p) as zf:
                    for name in zf.namelist():
                        if not name.lower().endswith(".dcm"):
                            continue
                        data = zf.read(name)
                        try:
                            ds = pydicom.dcmread(pydicom.filebase.DicomBytesIO(data), force=True)
                            out.append(ds)
                        except Exception as exc:
                            log_system("Send", f"Skipped {name}: {exc}", level="warning")
            except Exception as exc:
                log_system("Send", f"Failed to read zip: {exc}", level="error")
        else:
            try:
                out.append(pydicom.dcmread(str(p), force=True))
            except Exception as exc:
                log_system("Send", f"Failed to read {p.name}: {exc}", level="error")
        return out

    def clear_uploads(self):
        self._uploaded = []
        self.upload_label.setText("(no files uploaded - using sample)")
        self.upload_label.setObjectName("muted")
        self.upload_label.style().polish(self.upload_label)

    def do_send(self):
        ip = self.ip.text().strip() or "127.0.0.1"
        port = self.port.value()
        ae = self.ae.text().strip() or "ANY-SCP"
        local_ae = self.get_local()["local_ae"]
        if self._uploaded:
            datasets = self._uploaded
        else:
            datasets = [SAMPLE_DATASETS[self.modality_combo.currentText()]]
        self.send_btn.setEnabled(False)
        self.status_label.setText(f"Sending {len(datasets)} object(s)...")
        self.status_label.setObjectName("status-info")
        self.status_label.style().polish(self.status_label)

        worker = Worker(scu_store, local_ae, ip, port, ae, datasets)
        worker.finished_with_result.connect(self._done)
        worker.finished.connect(worker.deleteLater)
        self._worker = worker
        worker.start()

    def _done(self, ok: bool, summary: str, details: str):
        self.send_btn.setEnabled(True)
        self.status_label.setText(summary)
        self.status_label.setObjectName("status-ok" if ok else "status-err")
        self.status_label.style().polish(self.status_label)
        self.details.setPlainText(details or summary)


class PrintTab(QWidget):
    def __init__(self, get_local):
        super().__init__()
        self.get_local = get_local
        self._uploaded: list[Dataset] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        title = QLabel("DICOM Print")
        title.setObjectName("title")
        subtitle = QLabel("Test sending print jobs to film printers or paper print servers.")
        subtitle.setObjectName("muted")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        dest, self.ip, self.port, self.ae = make_destination_box(104)
        layout.addWidget(dest)

        media_box = QGroupBox("Printer / Medium")
        ml = QGridLayout(media_box)
        self.query_btn = QPushButton("Query Printer")
        self.query_btn.clicked.connect(self.do_query)
        self.medium_combo = QComboBox()
        self.medium_combo.addItems(MEDIUM_TYPES)
        ml.addWidget(self.query_btn, 0, 0)
        ml.addWidget(QLabel("Medium Type"), 0, 1)
        ml.addWidget(self.medium_combo, 0, 2)
        self.printer_label = QLabel("Printer status: unknown - select a medium manually if query fails")
        self.printer_label.setObjectName("muted")
        ml.addWidget(self.printer_label, 1, 0, 1, 3)
        ml.setColumnStretch(2, 1)
        layout.addWidget(media_box)

        src_box = QGroupBox("Source")
        sl = QGridLayout(src_box)
        sl.addWidget(QLabel("Built-in sample"), 0, 0)
        self.modality_combo = QComboBox()
        self.modality_combo.addItems(MODALITIES)
        sl.addWidget(self.modality_combo, 0, 1)
        self.upload_btn = QPushButton("Upload .dcm...")
        self.upload_btn.clicked.connect(self.pick_file)
        sl.addWidget(self.upload_btn, 0, 2)
        self.upload_label = QLabel("(no file uploaded - using sample)")
        self.upload_label.setObjectName("muted")
        sl.addWidget(self.upload_label, 1, 0, 1, 3)
        sl.setColumnStretch(1, 1)
        layout.addWidget(src_box)

        btn_row = QHBoxLayout()
        self.print_btn = QPushButton("Send Print Job")
        self.print_btn.setObjectName("primary")
        self.print_btn.clicked.connect(self.do_print)
        self.status_label = QLabel("Idle")
        self.status_label.setObjectName("muted")
        btn_row.addWidget(self.print_btn)
        btn_row.addWidget(self.status_label)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        result_group = QGroupBox("Result")
        rg_layout = QVBoxLayout(result_group)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        rg_layout.addWidget(self.details)
        layout.addWidget(result_group, 1)

    def pick_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select DICOM file", "", "DICOM (*.dcm);;All Files (*)"
        )
        if not path:
            return
        try:
            ds = pydicom.dcmread(path, force=True)
            self._uploaded = [ds]
            self.upload_label.setText(f"Uploaded: {Path(path).name}")
            self.upload_label.setObjectName("status-ok")
        except Exception as exc:
            self.upload_label.setText(f"Failed to load: {exc}")
            self.upload_label.setObjectName("status-err")
        self.upload_label.style().polish(self.upload_label)

    def do_query(self):
        ip = self.ip.text().strip() or "127.0.0.1"
        port = self.port.value()
        ae = self.ae.text().strip() or "ANY-SCP"
        local_ae = self.get_local()["local_ae"]
        self.query_btn.setEnabled(False)
        self.printer_label.setText("Querying printer...")
        self.printer_label.setObjectName("status-info")
        self.printer_label.style().polish(self.printer_label)

        worker = Worker(scu_query_printer, local_ae, ip, port, ae)
        worker.finished_with_result.connect(self._query_done)
        worker.finished.connect(worker.deleteLater)
        self._query_worker = worker
        worker.start()

    def _query_done(self, ok: bool, summary: str, details: str):
        self.query_btn.setEnabled(True)
        self.printer_label.setText(summary)
        self.printer_label.setObjectName("status-ok" if ok else "status-err")
        self.printer_label.style().polish(self.printer_label)

    def do_print(self):
        ip = self.ip.text().strip() or "127.0.0.1"
        port = self.port.value()
        ae = self.ae.text().strip() or "ANY-SCP"
        local_ae = self.get_local()["local_ae"]
        medium = self.medium_combo.currentText()
        if self._uploaded:
            ds = self._uploaded[0]
        else:
            ds = SAMPLE_DATASETS[self.modality_combo.currentText()]
        self.print_btn.setEnabled(False)
        self.status_label.setText("Sending print job...")
        self.status_label.setObjectName("status-info")
        self.status_label.style().polish(self.status_label)

        worker = Worker(scu_print, local_ae, ip, port, ae, ds, medium)
        worker.finished_with_result.connect(self._print_done)
        worker.finished.connect(worker.deleteLater)
        self._print_worker = worker
        worker.start()

    def _print_done(self, ok: bool, summary: str, details: str):
        self.print_btn.setEnabled(True)
        self.status_label.setText(summary)
        self.status_label.setObjectName("status-ok" if ok else "status-err")
        self.status_label.style().polish(self.status_label)
        self.details.setPlainText(details or summary)


class LogsTab(QWidget):
    COLS = ["", "Time", "Source", "Message"]

    def __init__(self, get_theme):
        super().__init__()
        self.get_theme = get_theme
        self._entries: list[dict] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        top = QHBoxLayout()
        title = QLabel("Network activity log")
        title.setObjectName("title")
        top.addWidget(title)
        top.addStretch(1)
        self.filter_combo = QComboBox()
        self.filter_combo.addItems(["All", "Outgoing", "Incoming", "Errors", "System"])
        self.filter_combo.currentIndexChanged.connect(self._refresh)
        top.addWidget(QLabel("Filter:"))
        top.addWidget(self.filter_combo)
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self.clear)
        top.addWidget(clear_btn)
        layout.addLayout(top)

        splitter = QSplitter(Qt.Vertical)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 24)
        self.table.setColumnWidth(1, 160)
        self.table.setColumnWidth(2, 130)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.itemSelectionChanged.connect(self._show_details)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(22)
        splitter.addWidget(self.table)

        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setPlaceholderText("Select a row to see details.")
        splitter.addWidget(self.details)
        splitter.setSizes([550, 200])
        layout.addWidget(splitter, 1)

        LOG_BUS.log.connect(self._on_log)

    def clear(self):
        self._entries.clear()
        self.table.setRowCount(0)
        self.details.clear()

    @Slot(dict)
    def _on_log(self, entry: dict):
        self._entries.append(entry)
        if len(self._entries) > 5000:
            self._entries = self._entries[-3000:]
            self._refresh()
            return
        if self._matches_filter(entry):
            self._append_row(entry)

    def _matches_filter(self, entry: dict) -> bool:
        f = self.filter_combo.currentText()
        if f == "All":
            return True
        if f == "Outgoing":
            return entry["direction"] == "out"
        if f == "Incoming":
            return entry["direction"] == "in"
        if f == "Errors":
            return entry["level"] in ("error", "warning")
        if f == "System":
            return entry["direction"] == "system"
        return True

    def _refresh(self):
        self.table.setRowCount(0)
        for e in self._entries:
            if self._matches_filter(e):
                self._append_row(e)

    def _append_row(self, entry: dict):
        theme = self.get_theme()
        r = self.table.rowCount()
        self.table.insertRow(r)
        # Direction icon column - small colored dot via background
        icon_item = QTableWidgetItem("")
        if entry["direction"] == "out":
            icon_item.setText("●")
            icon_item.setForeground(QColor(theme["out"]))
        elif entry["direction"] == "in":
            icon_item.setText("●")
            icon_item.setForeground(QColor(theme["in"]))
        elif entry["direction"] == "system":
            icon_item.setText("■")
            icon_item.setForeground(QColor(theme["muted"]))
        else:
            icon_item.setText("○")
            icon_item.setForeground(QColor(theme["muted"]))
        icon_item.setTextAlignment(Qt.AlignCenter)
        self.table.setItem(r, 0, icon_item)

        ts_item = QTableWidgetItem(entry["ts"])
        src_item = QTableWidgetItem(entry["source"])
        msg_item = QTableWidgetItem(entry["message"])

        if entry["level"] == "success":
            color = QColor(theme["ok"])
            for it in (ts_item, src_item, msg_item):
                it.setForeground(color)
        elif entry["level"] == "error":
            color = QColor(theme["err"])
            for it in (ts_item, src_item, msg_item):
                it.setForeground(color)
        elif entry["level"] == "warning":
            color = QColor(theme["warn"])
            for it in (ts_item, src_item, msg_item):
                it.setForeground(color)

        # Stash full entry on the first item for details view
        ts_item.setData(Qt.UserRole, entry)

        self.table.setItem(r, 1, ts_item)
        self.table.setItem(r, 2, src_item)
        self.table.setItem(r, 3, msg_item)
        self.table.scrollToBottom()

    def _show_details(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        item = self.table.item(rows[0].row(), 1)
        if not item:
            return
        entry = item.data(Qt.UserRole)
        if not entry:
            return
        text = (
            f"[{entry['ts']}] {entry['direction'].upper()}/{entry['level'].upper()}  "
            f"{entry['source']}\n"
            f"{entry['message']}"
        )
        if entry.get("details"):
            text += "\n\n" + entry["details"]
        self.details.setPlainText(text)


class SettingsTab(QWidget):
    def __init__(self, cfg: dict, on_change):
        super().__init__()
        self.cfg = cfg
        self.on_change = on_change
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        title = QLabel("Settings")
        title.setObjectName("title")
        layout.addWidget(title)

        local_box = QGroupBox("Local DICOM listener")
        form = QFormLayout(local_box)
        self.ae_input = QLineEdit(cfg["local_ae"])
        self.port_input = QSpinBox()
        self.port_input.setRange(1, 65535)
        self.port_input.setValue(int(cfg["local_port"]))
        form.addRow("Local AE Title", self.ae_input)
        form.addRow("Local Port", self.port_input)
        info = QLabel(
            "These determine how this instance is contacted by other DICOM nodes "
            "(echo, MWL, store, print). Two dicom.flux instances on the same network "
            "can fully test each other."
        )
        info.setObjectName("muted")
        info.setWordWrap(True)
        form.addRow(info)
        layout.addWidget(local_box)

        theme_box = QGroupBox("Appearance")
        tform = QFormLayout(theme_box)
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(list(THEMES.keys()))
        self.theme_combo.setCurrentText(cfg["theme"])
        tform.addRow("Theme", self.theme_combo)
        layout.addWidget(theme_box)

        btn_row = QHBoxLayout()
        save_btn = QPushButton("Apply & Save")
        save_btn.setObjectName("primary")
        save_btn.clicked.connect(self.apply)
        regen_btn = QPushButton("Regenerate Worklist Data")
        regen_btn.clicked.connect(self.regen)
        btn_row.addWidget(save_btn)
        btn_row.addWidget(regen_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        about = QGroupBox("About")
        a_layout = QVBoxLayout(about)
        a_layout.addWidget(QLabel(f"{APP_NAME} v{APP_VERSION}"))
        a_layout.addWidget(QLabel("Portable DICOM tester - Echo / MWL / Store / Print + built-in SCP."))
        layout.addWidget(about)
        layout.addStretch(1)

    def apply(self):
        self.cfg["local_ae"] = self.ae_input.text().strip().upper() or "DICOMFLUX"
        self.cfg["local_port"] = int(self.port_input.value())
        self.cfg["theme"] = self.theme_combo.currentText()
        save_config(self.cfg)
        self.on_change()

    def regen(self):
        init_mwl_entries()
        log_system("Settings", f"Regenerated {len(MWL_ENTRIES)} dummy worklist entries", level="success")


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self, cfg: dict):
        super().__init__()
        self.cfg = cfg
        self.setWindowTitle(f"{APP_NAME}  v{APP_VERSION}")
        self.resize(1100, 760)

        self.tabs = QTabWidget()
        get_local = lambda: self.cfg
        get_theme = lambda: THEMES[self.cfg["theme"]]
        self.echo_tab = EchoTab(get_local)
        self.mwl_tab = MWLTab(get_local)
        self.send_tab = SendTab(get_local)
        self.print_tab = PrintTab(get_local)
        self.logs_tab = LogsTab(get_theme)
        self.settings_tab = SettingsTab(self.cfg, self.apply_settings)

        self.tabs.addTab(self.echo_tab, "Echo")
        self.tabs.addTab(self.mwl_tab, "Modality Worklist")
        self.tabs.addTab(self.send_tab, "Send DICOM")
        self.tabs.addTab(self.print_tab, "Print")
        self.tabs.addTab(self.logs_tab, "Logs")
        self.tabs.addTab(self.settings_tab, "Settings")
        self.setCentralWidget(self.tabs)

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self._scp_label = QLabel("")
        self.status.addPermanentWidget(self._scp_label)
        self._update_status()

        self.apply_theme()
        SCP.start(self.cfg["local_port"], self.cfg["local_ae"])

    def apply_settings(self):
        self.apply_theme()
        SCP.start(self.cfg["local_port"], self.cfg["local_ae"])
        self._update_status()

    def apply_theme(self):
        theme = THEMES[self.cfg["theme"]]
        QApplication.instance().setStyleSheet(build_qss(theme))

    def _update_status(self):
        self._scp_label.setText(
            f"  Listening: AE='{self.cfg['local_ae']}' port {self.cfg['local_port']}  "
        )

    def closeEvent(self, event):
        SCP.stop()
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    install_pynetdicom_log_bridge()
    init_sample_datasets()
    init_mwl_entries()

    cfg = load_config()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName(APP_NAME)

    win = MainWindow(cfg)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
