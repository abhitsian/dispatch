"""Native macOS window for the dispatch dashboard.

We host the existing HTML/JS dashboard inside a WKWebView wrapped in an
NSWindow — so users get a real Mac app window (traffic lights, resize,
mission control space, Cmd-W to hide, Cmd-Q to quit) instead of a browser tab.

Why a webview and not native AppKit views: the dashboard already exists as
HTML/CSS/JS with a rich data model from /state. Re-implementing it as
NSCollectionView + NSTextView would be 10x the code for the same UX.
"""
from __future__ import annotations

from AppKit import (
    NSApplication,
    NSWindow,
    NSWindowStyleMaskTitled,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskResizable,
    NSBackingStoreBuffered,
    NSColor,
)
from Foundation import NSURL, NSURLRequest, NSMakeRect
from WebKit import WKWebView, WKWebViewConfiguration

# Keep a module-level reference so the window/view aren't garbage-collected.
_WINDOW = None
_WEBVIEW = None
_URL = None


def open_window(url: str):
    """Open (or re-show) the dashboard window. Must be called on the main thread."""
    global _WINDOW, _WEBVIEW, _URL
    _URL = url

    if _WINDOW is not None and _WINDOW.isVisible():
        _WINDOW.makeKeyAndOrderFront_(None)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        return _WINDOW

    rect = NSMakeRect(0, 0, 1180, 760)
    style = (
        NSWindowStyleMaskTitled
        | NSWindowStyleMaskClosable
        | NSWindowStyleMaskMiniaturizable
        | NSWindowStyleMaskResizable
    )
    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        rect, style, NSBackingStoreBuffered, False
    )
    window.setTitle_("Dispatch")
    window.setMinSize_((720, 480))
    window.setReleasedWhenClosed_(False)  # so re-show works after close
    window.setBackgroundColor_(NSColor.colorWithCalibratedRed_green_blue_alpha_(
        15/255, 17/255, 21/255, 1.0))

    config = WKWebViewConfiguration.alloc().init()
    # Allow same-origin http://127.0.0.1 fetches from the dashboard JS.
    prefs = config.preferences()
    try:
        prefs.setValue_forKey_(True, "allowFileAccessFromFileURLs")
        prefs.setValue_forKey_(True, "developerExtrasEnabled")  # right-click → Inspect
    except Exception:
        pass
    content = window.contentView()
    webview = WKWebView.alloc().initWithFrame_configuration_(
        content.bounds(), config)
    # auto-resize with the window: width-flexible + height-flexible
    webview.setAutoresizingMask_(0x12)  # NSViewWidthSizable | NSViewHeightSizable
    # Right-click → Inspect Element so JS errors are debuggable.
    try:
        webview.setValue_forKey_(True, "inspectable")
    except Exception:
        pass

    nsurl = NSURL.URLWithString_(url)
    req = NSURLRequest.requestWithURL_(nsurl)
    webview.loadRequest_(req)
    content.addSubview_(webview)

    window.center()
    window.makeKeyAndOrderFront_(None)
    NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

    _WINDOW = window
    _WEBVIEW = webview
    return window


def reopen():
    """Reopen the dashboard window using the last URL."""
    if _URL is None:
        return
    open_window(_URL)
