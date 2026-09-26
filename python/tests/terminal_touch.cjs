// ABOUTME: Exercises touch scrolling in both terminal pages using real xterm instances.
// ABOUTME: Checks gesture handling and records input sent over the session WebSocket.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const { chromium, webkit } = require("playwright");

const [BASE_URL, SESSION_ID, INPUT_PATH] = process.argv.slice(2);
const BROWSER = process.env.TERMWEB_BROWSER || "chromium";
const VIEWPORT = { width: 390, height: 844 };
const SETTLE_MS = 100;
const BROWSER_TIMEOUT_MS = 10000;
const INPUT_POLL_MS = 20;
const SWIPE_LINES = 6;
const MODES = [
  { name: "alternate", sequence: "\x1b[?1049h", up: "\x1b[5~", down: "\x1b[6~" },
  { name: "application cursor", sequence: "\x1b[?1049h\x1b[?1h", up: "\x1b[5~", down: "\x1b[6~" },
  { name: "alternate mouse", sequence: "\x1b[?1049h\x1b[?1000h\x1b[?1006h", mouse: true },
  { name: "normal mouse", sequence: "\x1b[?1002h\x1b[?1006h", mouse: true }
];

async function touch(page, type, points) {
  await page.evaluate(({ type, points }) => {
    const target = terminal.element.querySelector(".xterm-screen");
    const touches = points.map((point, identifier) => ({
      identifier, target, clientX: point.x, clientY: point.y,
      pageX: point.x + window.scrollX, pageY: point.y + window.scrollY
    }));
    const event = new Event(type, { bubbles: true, cancelable: true });
    Object.assign(event, { touches, targetTouches: touches, changedTouches: touches });
    target.dispatchEvent(event);
  }, { type, points });
}

async function swipe(page, start, delta, steps = 1) {
  await touch(page, "touchstart", [start]);
  for (let step = 1; step <= steps; step++) {
    await touch(page, "touchmove", [{ x: start.x, y: start.y + delta * step / steps }]);
  }
  await touch(page, "touchend", []);
  await page.waitForTimeout(SETTLE_MS);
}

async function output(page, sequence = "") {
  const mode = sequence ? MODES.findIndex(item => item.sequence === sequence) + 2 : 1;
  const response = await page.request.post(BASE_URL + "/api/sessions/" + SESSION_ID + "/input", {
    data: { data: String.fromCharCode(mode) }
  });
  assert(response.ok());
  await page.waitForFunction(marker => {
    const buffer = terminal.buffer.active;
    return buffer.getLine(buffer.baseY + buffer.cursorY).translateToString().includes(marker);
  }, "__SCROLL_READY_" + mode + "__");
  await page.waitForTimeout(SETTLE_MS);
}

async function main() {
  const browser = await ({ chromium, webkit })[BROWSER].launch();
  let sent = "";
  let inputCursor = 0;
  try {
    for (const path of ["/", "/dashboard"]) {
      const page = await browser.newPage({ viewport: VIEWPORT, isMobile: true, hasTouch: true });
      page.setDefaultTimeout(BROWSER_TIMEOUT_MS);
      const errors = [];
      const scrollRequests = [];
      page.on("websocket", socket => socket.on("framesent", event => {
        if (Buffer.isBuffer(event.payload)) scrollRequests.push(JSON.parse(event.payload.toString()));
      }));
      page.on("pageerror", error => errors.push(error.message));
      page.on("console", message => {
        if (["warning", "error"].includes(message.type())) errors.push(message.text());
      });
      await page.goto(BASE_URL + path);
      if (path === "/dashboard") {
        await page.evaluate(sessionId => connectToSession(sessionId), SESSION_ID);
      }
      await page.waitForFunction(() => terminal && ws && ws.readyState === WebSocket.OPEN);
      await output(page);
      const bounds = await page.locator(".xterm-screen").boundingBox();
      const rowHeight = bounds.height / await page.evaluate(() => terminal.rows);
      const start = { x: bounds.x + bounds.width / 2, y: bounds.y + bounds.height / 2 };
      const distance = rowHeight * (SWIPE_LINES + 0.25);
      const received = async (minimumLength = 0) => {
        const deadline = Date.now() + BROWSER_TIMEOUT_MS;
        let input = fs.readFileSync(INPUT_PATH, "utf8");
        while (input.length - inputCursor < minimumLength && Date.now() < deadline) {
          await page.waitForTimeout(INPUT_POLL_MS);
          input = fs.readFileSync(INPUT_PATH, "utf8");
        }
        const data = input.slice(inputCursor);
        inputCursor = input.length;
        return data;
      };

      const before = await page.evaluate(() => terminal.buffer.active.viewportY);
      await swipe(page, start, distance);
      assert.equal(await page.evaluate(() => terminal.buffer.active.viewportY), before - SWIPE_LINES,
        path + " ordinary scrollback must scroll once");
      assert.equal(await received(), "", "scrollback must not send keystrokes");

      for (const mode of MODES) {
        await output(page, mode.sequence);
        for (const direction of [1, -1]) {
          await swipe(page, start, distance * direction);
          const data = await received(1);
          assert.notEqual(data, "", path + " " + mode.name + " swipe must send scrolling input");
          if (mode.mouse) {
            const reports = data.match(/\x1b\[<(64|65);\d+;\d+M/g) || [];
            assert.equal(reports.length, SWIPE_LINES);
            assert.equal(reports.join(""), data);
            assert(reports.every(report => report.startsWith("\x1b[<" + (direction > 0 ? 64 : 65) + ";")));
          } else {
            assert.equal(data, direction > 0 ? mode.up : mode.down,
              "swipes page through displayed text without sending prompt-history arrow keys");
          }
          sent += data;
        }
      }

      await output(page, MODES[0].sequence);
      scrollRequests.length = 0;
      await swipe(page, start, distance, 30);
      const partial = await received(1);
      assert.equal(partial, MODES[0].up, "one swipe sends one page key across multiple movements");
      sent += partial;
      assert(scrollRequests.length > 1, "finger movement must produce incremental scroll requests");
      assert.equal(scrollRequests.reduce((total, request) => total + request.lines, 0), -SWIPE_LINES);
      assert.equal(scrollRequests.filter(request => request.start).length, 1);
      assert(scrollRequests.every(request => request.type === "scroll"));

      await touch(page, "touchstart", [start]);
      await touch(page, "touchend", []);
      await swipe(page, start, 2);
      await touch(page, "touchstart", [start]);
      await touch(page, "touchmove", [{ x: start.x + distance, y: start.y + 2 }]);
      await touch(page, "touchend", []);
      await touch(page, "touchstart", [start, { x: start.x + 20, y: start.y }]);
      await touch(page, "touchmove", [{ x: start.x, y: start.y + distance }]);
      await touch(page, "touchend", []);
      await touch(page, "touchstart", [start]);
      await touch(page, "touchcancel", []);
      await touch(page, "touchmove", [{ x: start.x, y: start.y + distance }]);
      await touch(page, "touchend", []);
      assert.equal(await received(), "", "taps, horizontal, multiple touches and canceled gestures send no input");
      assert.deepEqual(errors, []);
      await page.close();
    }
    process.stdout.write(JSON.stringify(sent));
  } finally {
    await browser.close();
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
