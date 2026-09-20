// Limits apply before CDP capture in CSS pixels * scale, and again to actual
// PNG pixel dimensions (including device scale). Encoded data is capped before
// decoding even its header. No failed enhanced capture falls back to a viewport.
const SCREENSHOT_LIMITS = Object.freeze({
  MAX_DIMENSION: 16384,
  MAX_PIXELS: 32000000,
  MAX_COORDINATE: 1000000,
  MAX_BASE64_LENGTH: 32 * 1024 * 1024,
  MAX_SCALE: 2,
});

function screenshotDimensions(width, height) {
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0
      || width > SCREENSHOT_LIMITS.MAX_DIMENSION || height > SCREENSHOT_LIMITS.MAX_DIMENSION
      || width * height > SCREENSHOT_LIMITS.MAX_PIXELS) {
    throw new Error("Screenshot exceeds pixel limits (16384 per dimension, 32000000 pixels)");
  }
}

function screenshotRect(rect) {
  if (!rect || typeof rect !== "object" || Array.isArray(rect)) {
    throw new Error("Screenshot region must be {x,y,width,height} in CSS page coordinates");
  }
  for (const key of ["x", "y", "width", "height"]) {
    const value = rect[key];
    if (typeof value !== "number" || !Number.isFinite(value) || value < 0
        || value > SCREENSHOT_LIMITS.MAX_COORDINATE
        || ((key === "width" || key === "height") && value === 0)) {
      throw new Error(`Invalid screenshot ${key}: expected finite, bounded CSS coordinates`);
    }
  }
  if (rect.x + rect.width > SCREENSHOT_LIMITS.MAX_COORDINATE
      || rect.y + rect.height > SCREENSHOT_LIMITS.MAX_COORDINATE) {
    throw new Error("Screenshot region exceeds CSS coordinate limits");
  }
  return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
}

function screenshotPNG(data) {
  if (typeof data !== "string" || data.length > SCREENSHOT_LIMITS.MAX_BASE64_LENGTH) {
    throw new Error("Screenshot exceeds encoded size limit (32 MiB base64)");
  }
  if (data.length < 60 || data.length % 4 || !/^[A-Za-z0-9+/]*={0,2}$/.test(data)) {
    throw new Error("Screenshot returned invalid PNG base64");
  }
  const header = atob(data.slice(0, 44));
  const tail = atob(data.slice(-24));
  const u32 = (offset) => ((header.charCodeAt(offset) * 0x1000000)
    + (header.charCodeAt(offset + 1) << 16)
    + (header.charCodeAt(offset + 2) << 8) + header.charCodeAt(offset + 3));
  if (header.slice(0, 8) !== "\x89PNG\r\n\x1a\n" || u32(8) !== 13
      || header.slice(12, 16) !== "IHDR"
      || tail.slice(-12) !== "\0\0\0\0IEND\xae\x42\x60\x82") {
    throw new Error("Screenshot returned invalid PNG data");
  }
  screenshotDimensions(u32(16), u32(20));
  return `data:image/png;base64,${data}`;
}

async function captureScreenshot(a) {
  if (!a || !Number.isSafeInteger(a.tabId) || a.tabId < 0) {
    throw new Error("Screenshot tabId must be a nonnegative safe integer");
  }
  if (a.fullPage !== undefined && typeof a.fullPage !== "boolean") {
    throw new Error("Screenshot fullPage must be a boolean");
  }
  const scale = a.scale === undefined ? 1 : a.scale;
  if (typeof scale !== "number" || !Number.isFinite(scale) || scale <= 0
      || scale > SCREENSHOT_LIMITS.MAX_SCALE) {
    throw new Error("Screenshot scale must be finite and greater than 0, at most 2");
  }
  if (a.fullPage && a.region !== undefined) {
    throw new Error("Screenshot fullPage and region are mutually exclusive");
  }
  const region = a.region === undefined ? null : screenshotRect(a.region);
  if (region) screenshotDimensions(Math.ceil(region.width * scale), Math.ceil(region.height * scale));

  if (!a.fullPage && !region && scale === 1) {
    const tab = await chrome.tabs.get(a.tabId);
    await chrome.tabs.update(a.tabId, { active: true });
    const dataURL = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "png" });
    const prefix = "data:image/png;base64,";
    if (typeof dataURL !== "string" || !dataURL.startsWith(prefix)) {
      throw new Error("Screenshot returned an invalid PNG data URL");
    }
    return screenshotPNG(dataURL.slice(prefix.length));
  }

  const lease = await cdpSessions.acquire(a.tabId);
  try {
    const metrics = await cdpSessions.send(a.tabId, "Page.getLayoutMetrics", {}, lease);
    let clip;
    if (region) {
      clip = region;
    } else if (a.fullPage) {
      // Deprecated contentSize is in device pixels; never silently interpret it
      // as CSS coordinates on older Chromium versions.
      clip = screenshotRect(metrics.cssContentSize);
    } else {
      const viewport = metrics.cssVisualViewport;
      clip = screenshotRect(viewport && {
        x: viewport.pageX, y: viewport.pageY,
        width: viewport.clientWidth, height: viewport.clientHeight,
      });
    }
    screenshotDimensions(Math.ceil(clip.width * scale), Math.ceil(clip.height * scale));
    const result = await cdpSessions.send(a.tabId, "Page.captureScreenshot", {
      format: "png",
      fromSurface: true,
      captureBeyondViewport: true,
      clip: { ...clip, scale },
    }, lease);
    return screenshotPNG(result?.data);
  } finally {
    cdpSessions.release(a.tabId, lease);
  }
}
