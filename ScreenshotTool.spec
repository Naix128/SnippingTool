# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs
import os


datas = []
binaries = []
hiddenimports = ["ctranslate2", "sentencepiece"]
excludes = []
binaries += collect_dynamic_libs("ctranslate2")
include_rapidocr = os.environ.get("SCREENSHOTTOOL_INCLUDE_RAPIDOCR") == "1"
if include_rapidocr:
    datas += collect_data_files(
        "rapidocr",
        includes=["config.yaml", "default_models.yaml", "models/*.onnx"],
    )
    hiddenimports += [
        "rapidocr",
        "rapidocr.main",
        "rapidocr.inference_engine.onnxruntime",
        "onnxruntime",
    ]
    excludes += [
        "IPython",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "_pytest",
        "comm",
        "ipykernel",
        "ipywidgets",
        "jedi",
        "jupyter_client",
        "matplotlib_inline",
        "pytest",
        "tqdm.notebook",
        "traitlets",
        "zmq",
        "onnxruntime.experimental",
        "rapidocr.inference_engine.mnn",
        "rapidocr.inference_engine.openvino",
        "rapidocr.inference_engine.paddle",
        "rapidocr.inference_engine.pytorch",
        "rapidocr.inference_engine.tensorrt",
    ]

app_name = "ScreenshotTool-AccurateOCR" if include_rapidocr else "ScreenshotTool"


a = Analysis(
    ['screenshot_app.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=app_name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets\\screenshot_tool.ico'],
)
