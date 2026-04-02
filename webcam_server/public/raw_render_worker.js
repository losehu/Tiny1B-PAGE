"use strict";

let THERMAL_W = 256;
let THERMAL_H = 192;
let PIXEL_COUNT = THERMAL_W * THERMAL_H;
let THERMAL_BYTES = PIXEL_COUNT * 2;

let colorNormMode = "adaptive"; // adaptive | real_custom
let realNormRawMin = Math.round((10 + 273.15) * 16);
let realNormRawRange = Math.max(1, Math.round((50 + 273.15) * 16) - realNormRawMin);
let activeLut = new Uint8Array(256 * 3);
for (let i = 0; i < 256; i++) {
  const o = i * 3;
  activeLut[o] = i;
  activeLut[o + 1] = i;
  activeLut[o + 2] = i;
}

let offscreen = null;
let ctx = null;
let imageData = null;
let frameRgba = null;

const littleEndian = (() => {
  const b = new ArrayBuffer(2);
  new DataView(b).setUint16(0, 0x00ff, true);
  return new Uint16Array(b)[0] === 0x00ff;
})();

function ensureSurface() {
  if (!offscreen || offscreen.width !== THERMAL_W || offscreen.height !== THERMAL_H) {
    offscreen = new OffscreenCanvas(THERMAL_W, THERMAL_H);
    ctx = offscreen.getContext("2d", { alpha: false, desynchronized: true });
    imageData = ctx.createImageData(THERMAL_W, THERMAL_H);
    frameRgba = imageData.data;
  }
}

function mapRawToPaletteIdx(v, minV, maxV) {
  if (colorNormMode === "real_custom") {
    const minRaw = realNormRawMin;
    const maxRaw = minRaw + realNormRawRange;
    const clamped = v < minRaw ? minRaw : (v > maxRaw ? maxRaw : v);
    return (((clamped - minRaw) * 255) / realNormRawRange) | 0;
  }
  const range = maxV - minV;
  return range > 0 ? (((v - minV) * 255) / range) | 0 : (v >>> 8);
}

async function renderFrame(rawBuffer, frameId, regions) {
  if (!(rawBuffer instanceof ArrayBuffer) || rawBuffer.byteLength !== THERMAL_BYTES) {
    return;
  }
  ensureSurface();

  const values = littleEndian ? new Uint16Array(rawBuffer) : null;
  const dv = littleEndian ? null : new DataView(rawBuffer);
  let minV = 65535;
  let maxV = 0;

  for (let i = 0; i < PIXEL_COUNT; i++) {
    const src = PIXEL_COUNT - 1 - i; // match yombir default rotate180
    const v = littleEndian ? values[src] : dv.getUint16(src * 2, true);
    if (v < minV) minV = v;
    if (v > maxV) maxV = v;
  }

  for (let i = 0; i < PIXEL_COUNT; i++) {
    const src = PIXEL_COUNT - 1 - i;
    const v = littleEndian ? values[src] : dv.getUint16(src * 2, true);
    const idx = mapRawToPaletteIdx(v, minV, maxV);
    const p = idx * 3;
    const o = i * 4;
    frameRgba[o] = activeLut[p];
    frameRgba[o + 1] = activeLut[p + 1];
    frameRgba[o + 2] = activeLut[p + 2];
    frameRgba[o + 3] = 255;
  }

  ctx.putImageData(imageData, 0, 0);

  let bitmap = null;
  if (typeof offscreen.transferToImageBitmap === "function") {
    bitmap = offscreen.transferToImageBitmap();
  } else {
    bitmap = await createImageBitmap(offscreen);
  }

  postMessage(
    {
      type: "bitmap",
      frameId,
      regions: Array.isArray(regions) ? regions : null,
      bitmap,
    },
    [bitmap]
  );
}

self.onmessage = async (ev) => {
  const msg = ev.data || {};

  if (msg.type === "config") {
    const w = Number.parseInt(msg.thermalW, 10);
    const h = Number.parseInt(msg.thermalH, 10);
    if (Number.isFinite(w) && w > 0) THERMAL_W = w;
    if (Number.isFinite(h) && h > 0) THERMAL_H = h;
    PIXEL_COUNT = THERMAL_W * THERMAL_H;
    THERMAL_BYTES = PIXEL_COUNT * 2;

    if (msg.colorNormMode === "adaptive" || msg.colorNormMode === "real_custom") {
      colorNormMode = msg.colorNormMode;
    }
    if (Number.isFinite(msg.realNormRawMin)) {
      realNormRawMin = Number(msg.realNormRawMin);
    }
    if (Number.isFinite(msg.realNormRawRange)) {
      realNormRawRange = Math.max(1, Number(msg.realNormRawRange));
    }

    if (msg.lut instanceof ArrayBuffer) {
      const lut = new Uint8Array(msg.lut);
      if (lut.byteLength === 256 * 3) {
        activeLut = lut;
      }
    }
    return;
  }

  if (msg.type === "frame") {
    try {
      await renderFrame(msg.raw, Number.isInteger(msg.frameId) ? msg.frameId : -1, msg.regions || null);
    } catch (_) {
      // Keep worker alive even if one frame fails.
    }
  }
};
