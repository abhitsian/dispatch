"""py2app setup for Dispatch.

Build a standalone Dispatch.app bundle:
    cd ~/claude-apps/dispatch
    .venv/bin/python setup.py py2app

The output lands at dist/Dispatch.app — drag to /Applications.
"""
from setuptools import setup

APP = ["app.py"]
DATA_FILES = [
    # everything that needs to be visible to the running app
    "dashboard.html",
    "allowlist.py",
    "agents.py",
    "audio.py",
    "dispatch.py",
    "dispatch_server.py",
    "install_hook.py",
    "native_window.py",
    "paths.py",
    "protocol.py",
    "sessions.py",
    "terminal_inject.py",
    ("hooks", ["hooks/pretooluse.sh"]),
    ("sounds", [
        "sounds/alert_tone.wav",
        "sounds/key_down.wav",
        "sounds/key_up.wav",
        "sounds/mic_click.wav",
        "sounds/roger_beep.wav",
        "sounds/squelch_tail.wav",
        "sounds/static_post.wav",
        "sounds/static_pre.wav",
    ]),
]

OPTIONS = {
    "argv_emulation": False,
    "iconfile": "Dispatch.icns",
    "plist": {
        "CFBundleName": "Dispatch",
        "CFBundleDisplayName": "Dispatch",
        "CFBundleIdentifier": "com.vaibhav.dispatch",
        "CFBundleVersion": "1.0.0",
        "CFBundleShortVersionString": "1.0",
        "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
        # WKWebView refuses http:// by default — explicitly allow loopback so the
        # dashboard can fetch http://127.0.0.1:8765/api/*
        "NSAppTransportSecurity": {
            "NSAllowsLocalNetworking": True,
            "NSAllowsArbitraryLoads": True,
        },
        "NSMicrophoneUsageDescription":
            "Dispatch needs microphone access so you can transmit radio "
            "messages to your Claude sessions.",
        "NSAppleEventsUsageDescription":
            "Dispatch types your transcribed voice messages into the right "
            "terminal so they appear in your chat window and execute.",
    },
    # _sounddevice_data ships libportaudio.dylib; if py2app zips it, dlopen
    # fails at runtime. Listing the package here keeps it unzipped.
    # NOTE: mlx / mlx_whisper deliberately excluded — py2app's static walker
    # blows the recursion limit on mlx. The fast path is enabled when running
    # from the source tree (which has the venv); the bundled .app uses the
    # openai-whisper CLI fallback.
    "packages": ["rumps", "sounddevice", "_sounddevice_data", "numpy"],
    "includes": [
        "queue", "json", "http.server", "uuid", "threading", "subprocess",
        "wave", "tempfile", "fnmatch", "urllib.parse",
        # WKWebView host for the native window
        "WebKit", "AppKit", "Foundation", "PyObjCTools.AppHelper",
    ],
}

setup(
    app=APP,
    name="Dispatch",
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
