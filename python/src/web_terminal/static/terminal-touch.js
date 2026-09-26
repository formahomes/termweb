// ABOUTME: Converts single-finger terminal swipes into application scroll input.
// ABOUTME: Leaves ordinary scrollback to xterm and preserves taps and pinch gestures.
const TOUCH_SCROLL_THRESHOLD = 8;
const TOUCH_SCROLL_BUFFER = "alternate";
const TOUCH_SCROLL_MOUSE_DISABLED = "none";
const TOUCH_SCROLL_PAGE_UP = "\x1b[5~";
const TOUCH_SCROLL_PAGE_DOWN = "\x1b[6~";

function enableTouchScrolling(terminal, threshold = TOUCH_SCROLL_THRESHOLD) {
  const element = terminal.element;
  const screen = element.querySelector(".xterm-screen");
  let gesture = null;

  function scrollsApplication() {
    return terminal.buffer.active.type === TOUCH_SCROLL_BUFFER ||
      terminal.modes.mouseTrackingMode !== TOUCH_SCROLL_MOUSE_DISABLED;
  }

  element.addEventListener("touchstart", (event) => {
    gesture = null;
    if (event.touches.length !== 1 || !scrollsApplication()) return;
    const touch = event.touches[0];
    gesture = {
      identifier: touch.identifier,
      x: touch.clientX,
      y: touch.clientY,
      lastY: touch.clientY,
      remainder: 0,
      pageSent: false,
      scrolling: false
    };
  }, { capture: true, passive: true });

  element.addEventListener("touchmove", (event) => {
    if (!gesture) return;
    if (event.touches.length !== 1 || !scrollsApplication()) {
      gesture = null;
      return;
    }
    const touch = event.touches[0];
    if (touch.identifier !== gesture.identifier) {
      gesture = null;
      return;
    }
    if (!gesture.scrolling) {
      const horizontal = Math.abs(touch.clientX - gesture.x);
      const vertical = Math.abs(touch.clientY - gesture.y);
      if (Math.max(horizontal, vertical) < threshold) return;
      if (horizontal >= vertical) {
        gesture = null;
        return;
      }
      gesture.scrolling = true;
    }

    if (event.cancelable) event.preventDefault();
    event.stopImmediatePropagation();
    if (terminal.modes.mouseTrackingMode === TOUCH_SCROLL_MOUSE_DISABLED) {
      if (!gesture.pageSent) {
        terminal.input(touch.clientY > gesture.y ? TOUCH_SCROLL_PAGE_UP : TOUCH_SCROLL_PAGE_DOWN, true);
        gesture.pageSent = true;
      }
      return;
    }

    // xterm's wheel handler encodes mouse scroll reports for the active program.
    gesture.remainder += gesture.lastY - touch.clientY;
    gesture.lastY = touch.clientY;
    const rowHeight = screen.getBoundingClientRect().height / terminal.rows;
    if (rowHeight <= 0) return;
    const lines = Math.trunc(gesture.remainder / rowHeight);
    gesture.remainder -= lines * rowHeight;
    for (let line = 0; line < Math.abs(lines); line++) {
      screen.dispatchEvent(new WheelEvent("wheel", {
        bubbles: true,
        cancelable: true,
        deltaMode: WheelEvent.DOM_DELTA_LINE,
        deltaY: Math.sign(lines),
        clientX: gesture.x,
        clientY: gesture.y
      }));
    }
  }, { capture: true, passive: false });

  function endGesture() {
    gesture = null;
  }

  element.addEventListener("touchend", endGesture, { passive: true });
  element.addEventListener("touchcancel", endGesture, { passive: true });
}
