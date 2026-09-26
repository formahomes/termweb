// ABOUTME: Converts single-finger terminal swipes into application scroll input.
// ABOUTME: Leaves ordinary scrollback to xterm and preserves taps and pinch gestures.
const TOUCH_SCROLL_THRESHOLD = 8;
const TOUCH_SCROLL_BUFFER = "alternate";
const TOUCH_SCROLL_MOUSE_DISABLED = "none";

function enableTouchScrolling(terminal, getSocket, threshold = TOUCH_SCROLL_THRESHOLD) {
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
      scrollStarted: false,
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
    gesture.remainder += gesture.lastY - touch.clientY;
    gesture.lastY = touch.clientY;
    const bounds = screen.getBoundingClientRect();
    const rowHeight = bounds.height / terminal.rows;
    if (rowHeight <= 0) return;
    const lines = Math.trunc(gesture.remainder / rowHeight);
    if (lines === 0) return;
    gesture.remainder -= lines * rowHeight;
    if (terminal.modes.mouseTrackingMode === TOUCH_SCROLL_MOUSE_DISABLED) {
      const socket = getSocket();
      if (socket && socket.readyState === WebSocket.OPEN) {
        const column = Math.max(1, Math.min(terminal.cols,
          Math.floor((gesture.x - bounds.left) / (bounds.width / terminal.cols)) + 1));
        const row = Math.max(1, Math.min(terminal.rows,
          Math.floor((gesture.y - bounds.top) / rowHeight) + 1));
        socket.send(new TextEncoder().encode(JSON.stringify({
          type: "scroll", lines, column, row, start: !gesture.scrollStarted
        })));
        gesture.scrollStarted = true;
      }
      return;
    }

    // xterm's wheel handler encodes mouse scroll reports for the active program.
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
