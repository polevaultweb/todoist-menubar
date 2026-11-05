"""Floating focus window: the task, a Pomodoro countdown and its controls.

A small panel that stays above other windows on every Space (including full-screen apps) and
does not take focus from what you are typing in. Drag it anywhere; macOS remembers where.
It has a solid near-black fill, so every priority dot reads clearly, and a thick border coloured
by mode (tomato red for work, green for a break, slate when paused) so it stands out.
The timer itself lives in the app; this only displays it and forwards button clicks.
"""

import objc
from AppKit import (
    NSAppearance, NSBackingStoreBuffered, NSBezelStyleRounded, NSBox, NSBoxCustom, NSButton, NSColor, NSFloatingWindowLevel,
    NSFont, NSFontWeightMedium, NSFontWeightSemibold, NSLineBreakByTruncatingTail, NSLineBreakByWordWrapping, NSMakeRect, NSPanel, NSViewHeightSizable, NSViewWidthSizable,
    NSScreen, NSTextField, NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary, NSWindowCollectionBehaviorStationary,
    NSWindowStyleMaskClosable, NSWindowStyleMaskHUDWindow, NSWindowStyleMaskNonactivatingPanel,
    NSWindowStyleMaskTitled, NSWindowStyleMaskUtilityWindow,
)
from Foundation import NSObject

W, H = 340, 140

FILL = (0.11, 0.11, 0.13)
BORDER_WIDTH = 4

# Border colour per mode, as (r, g, b)
MODE_COLOURS = {
    "work": (0.90, 0.28, 0.20),    # tomato
    "break": (0.20, 0.70, 0.42),   # green
    "paused": (0.50, 0.54, 0.62),  # slate
}


def _rgb(r, g, b, a=1.0):
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, a)


def _label(text, frame, font, color=None, lines=1):
    field = NSTextField.wrappingLabelWithString_(text) if lines > 1 else NSTextField.labelWithString_(text)
    field.setFrame_(frame)
    field.setFont_(font)
    field.setMaximumNumberOfLines_(lines)
    field.setLineBreakMode_(NSLineBreakByWordWrapping if lines > 1 else NSLineBreakByTruncatingTail)
    field.cell().setTruncatesLastVisibleLine_(True)
    if color:
        field.setTextColor_(color)
    return field


class _ButtonTarget(NSObject):
    """Objective-C target for the buttons; calls actions[button identifier]"""

    def initWithActions_(self, actions):
        self = objc.super(_ButtonTarget, self).init()
        if self is None:
            return None
        self.actions = actions
        return self

    def clicked_(self, sender):
        self.actions[sender.identifier()]()


class FocusPanel:
    """Owns the panel. `actions` maps button names (toggle, extend, done, stop) to callables."""

    def __init__(self, actions):
        self.target = _ButtonTarget.alloc().initWithActions_(actions)

        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskUtilityWindow
                 | NSWindowStyleMaskHUDWindow | NSWindowStyleMaskNonactivatingPanel)
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, W, H), style, NSBackingStoreBuffered, False)
        panel.setTitle_("Focus")
        panel.setLevel_(NSFloatingWindowLevel)
        panel.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces
                                     | NSWindowCollectionBehaviorStationary
                                     | NSWindowCollectionBehaviorFullScreenAuxiliary)
        panel.setHidesOnDeactivate_(False)
        panel.setFloatingPanel_(True)
        panel.setBecomesKeyOnlyIfNeeded_(True)
        panel.setMovableByWindowBackground_(True)
        panel.setReleasedWhenClosed_(False)  # the close button only hides it; focus carries on
        panel.setAppearance_(NSAppearance.appearanceNamed_("NSAppearanceNameVibrantDark"))
        if panel.setFrameUsingName_("TodoistFocusPanel"):
            # The saved frame may be from an older size; keep its top-left corner, use the current size
            frame = panel.frame()
            top_left = (frame.origin.x, frame.origin.y + frame.size.height)
            panel.setContentSize_((W, H))
            panel.setFrameTopLeftPoint_(top_left)
        else:
            screen = NSScreen.mainScreen().visibleFrame()
            panel.setFrameTopLeftPoint_((screen.origin.x + screen.size.width - W - 24,
                                         screen.origin.y + screen.size.height - 24))
        panel.setFrameAutosaveName_("TodoistFocusPanel")
        self.panel = panel

        view = panel.contentView()
        self.background = NSBox.alloc().initWithFrame_(view.bounds())
        self.background.setBoxType_(NSBoxCustom)
        self.background.setFillColor_(_rgb(*FILL))
        self.background.setBorderWidth_(BORDER_WIDTH)
        self.background.setCornerRadius_(6)
        self.background.setTitlePosition_(0)  # NSNoTitle
        self.background.setContentViewMargins_((0, 0))
        self.background.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        view.addSubview_(self.background)
        self.mode = None
        white = NSColor.whiteColor()
        self.task = _label("", NSMakeRect(14, H - 52, W - 28, 40),
                           NSFont.systemFontOfSize_weight_(14, NSFontWeightSemibold), white, lines=2)
        self.clock = _label("🍅 25:00", NSMakeRect(12, 42, 190, 46),
                            NSFont.monospacedDigitSystemFontOfSize_weight_(38, NSFontWeightMedium), white)
        self.phase = _label("", NSMakeRect(206, 56, W - 218, 18), NSFont.systemFontOfSize_weight_(11, NSFontWeightMedium),
                            _rgb(1, 1, 1, 0.85))
        for v in (self.task, self.clock, self.phase):
            view.addSubview_(v)

        self.buttons = {}
        x = 12
        for name, title, width in (("toggle", "Pause", 78), ("extend", "+5 min", 78),
                                   ("done", "Done ✓", 84), ("stop", "Stop", 72)):
            b = NSButton.buttonWithTitle_target_action_(title, self.target, "clicked:")
            b.setFrame_(NSMakeRect(x, 10, width, 28))
            b.setBezelStyle_(NSBezelStyleRounded)
            b.setIdentifier_(name)
            view.addSubview_(b)
            self.buttons[name] = b
            x += width + 4
        self.buttons["done"].setToolTip_("Complete the task in Todoist and end the focus")
        self.buttons["stop"].setToolTip_("End the focus; the task stays open")

    def update(self, task, clock, phase, toggle_title, mode="work"):
        """mode is a MODE_COLOURS key: work, break or paused"""
        if mode != self.mode:
            self.mode = mode
            self.background.setBorderColor_(_rgb(*MODE_COLOURS[mode]))
        if self.task.stringValue() != task:
            self.task.setStringValue_(task)
            self.task.setToolTip_(task)
        self.clock.setStringValue_(clock)
        self.phase.setStringValue_(phase)
        if self.buttons["toggle"].title() != toggle_title:
            self.buttons["toggle"].setTitle_(toggle_title)

    def show(self):
        self.panel.orderFrontRegardless()

    def hide(self):
        self.panel.orderOut_(None)

    def visible(self):
        return bool(self.panel.isVisible())
