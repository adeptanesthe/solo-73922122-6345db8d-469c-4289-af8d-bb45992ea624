/*
 * zip-restore.js — tiny dependency-free ZIP reader + PCM8/16 WAV restorer.
 *
 * The segment page fetches the exact generated ZIP and restores A/B here in
 * the browser (no server-side audio for the main preview path).  DEFLATE is
 * decoded with the runtime's native DecompressionStream("deflate-raw")
 * (Chromium / Firefox / Safari 16.4+ / Node 18+).
 *
 * The UMD-ish wrapper also lets tests/zip_restore_check.mjs require it in
 * Node and cross-check Python zipfile-produced archives.
 */
(function (root, factory) {
  "use strict";
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.ZipRestore = factory();
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function supported() {
    return typeof globalThis.DecompressionStream === "function";
  }

  function dv(u8) {
    return new DataView(u8.buffer, u8.byteOffset, u8.byteLength);
  }

  // Find the last End-Of-Central-Directory record (handles ZIP comments).
  function findEocd(view, len) {
    var min = Math.max(0, len - 22 - 0xffff);
    for (var off = len - 22; off >= min; off--) {
      if (view.getUint32(off, true) === 0x06054b50) return off;
    }
    throw new Error("找不到 ZIP 中央目录结尾（EOCD），文件可能不是 ZIP");
  }

  async function inflateRaw(u8) {
    if (!supported()) {
      throw new Error("当前浏览器不支持 DecompressionStream deflate-raw，无法在网页内解 ZIP");
    }
    var ds = new globalThis.DecompressionStream("deflate-raw");
    var blob = new Blob([u8]);
    var stream = blob.stream().pipeThrough(ds);
    return new Uint8Array(await new Response(stream).arrayBuffer());
  }

  // Parse the whole ZIP; returns Map<forward-slash name, Uint8Array inflated>.
  async function unzip(buffer) {
    var bytes = buffer instanceof Uint8Array ? buffer : new Uint8Array(buffer);
    var view = dv(bytes);
    var eocd = findEocd(view, bytes.length);
    var count = view.getUint16(eocd + 10, true);
    var cdOffset = view.getUint32(eocd + 16, true);

    var out = new Map();
    var p = cdOffset;
    var decoder = new TextDecoder("utf-8");

    for (var n = 0; n < count; n++) {
      if (view.getUint32(p, true) !== 0x02014b50) {
        throw new Error("ZIP 中央目录头损坏");
      }
      var method = view.getUint16(p + 10, true);
      var compSize = view.getUint32(p + 20, true);
      var nameLen = view.getUint16(p + 28, true);
      var extraLen = view.getUint16(p + 30, true);
      var commentLen = view.getUint16(p + 32, true);
      var localOff = view.getUint32(p + 42, true);
      var name = decoder.decode(bytes.subarray(p + 46, p + 46 + nameLen));

      // Data start must be taken from the *local* header (its extra length
      // can differ from the central directory's).
      if (view.getUint32(localOff, true) !== 0x04034b50) {
        throw new Error("ZIP 本地文件头损坏：" + name);
      }
      var localNameLen = view.getUint16(localOff + 26, true);
      var localExtraLen = view.getUint16(localOff + 28, true);
      var dataStart = localOff + 30 + localNameLen + localExtraLen;
      var raw = bytes.subarray(dataStart, dataStart + compSize);

      var data;
      if (method === 0) {
        data = raw.slice();
      } else if (method === 8) {
        data = await inflateRaw(raw);
      } else {
        throw new Error("不支持的 ZIP 压缩方式 " + method + "（" + name + "）");
      }
      out.set(name, data);
      p += 46 + nameLen + extraLen + commentLen;
    }
    return out;
  }

  // Minimal RIFF/WAVE parser for our own mono 8/16-bit PCM chunk WAVs.
  function parseWav(u8) {
    if (u8.length < 44 ||
        u8[0] !== 0x52 || u8[1] !== 0x49 || u8[2] !== 0x46 || u8[3] !== 0x46) {
      throw new Error("分块不是 RIFF/WAVE");
    }
    var view = dv(u8);
    var pos = 12;
    var fmt = null;
    var dataPos = -1;
    var dataSize = 0;
    while (pos + 8 <= u8.length) {
      var id = String.fromCharCode(u8[pos], u8[pos + 1], u8[pos + 2], u8[pos + 3]);
      var size = view.getUint32(pos + 4, true);
      var body = pos + 8;
      if (id === "fmt ") {
        fmt = {
          tag: view.getUint16(body, true),
          channels: view.getUint16(body + 2, true),
          sampleRate: view.getUint32(body + 4, true),
          bits: view.getUint16(body + 14, true)
        };
      } else if (id === "data") {
        dataPos = body;
        dataSize = Math.min(size, u8.length - body);
      }
      pos = body + size + (size & 1);
    }
    if (!fmt || dataPos < 0) throw new Error("WAV 缺少 fmt/data 块");
    if (fmt.channels !== 1) throw new Error("只支持单声道分块");
    if (fmt.tag !== 1 && fmt.tag !== 0xfffe) throw new Error("只支持 PCM 分块");

    var samples, i;
    if (fmt.bits === 16) {
      var n16 = dataSize >> 1;
      samples = new Int16Array(n16);
      // Read through the DataView so odd chunk offsets stay safe.
      for (i = 0; i < n16; i++) samples[i] = view.getInt16(dataPos + i * 2, true);
    } else if (fmt.bits === 8) {
      samples = new Int16Array(dataSize);
      for (i = 0; i < dataSize; i++) samples[i] = (u8[dataPos + i] - 128) << 8;
    } else {
      throw new Error("网页还原只支持 8/16 bit 分块，检测到 " + fmt.bits + " bit");
    }
    return { sampleRate: fmt.sampleRate, bits: fmt.bits, samples: samples };
  }

  // Encode mono Int16 samples as a standard PCM16 WAV byte array.
  function encodePcm16Wav(samples, sampleRate) {
    var n = samples.length;
    var buf = new ArrayBuffer(44 + n * 2);
    var u8 = new Uint8Array(buf);
    var v = new DataView(buf);
    function fourcc(off, s) {
      for (var i = 0; i < 4; i++) u8[off + i] = s.charCodeAt(i);
    }
    fourcc(0, "RIFF");
    v.setUint32(4, 36 + n * 2, true);
    fourcc(8, "WAVE");
    fourcc(12, "fmt ");
    v.setUint32(16, 16, true);
    v.setUint16(20, 1, true);        // PCM
    v.setUint16(22, 1, true);        // mono
    v.setUint32(24, sampleRate, true);
    v.setUint32(28, sampleRate * 2, true);
    v.setUint16(32, 2, true);
    v.setUint16(34, 16, true);
    fourcc(36, "data");
    v.setUint32(40, n * 2, true);
    var off = 44;
    for (var k = 0; k < n; k++, off += 2) v.setInt16(off, samples[k], true);
    return u8;
  }

  function concatInt16(parts, total) {
    var all = new Int16Array(total);
    var off = 0;
    parts.forEach(function (p) { all.set(p, off); off += p.length; });
    return all;
  }

  /*
   * Restore a generated package ZIP (ArrayBuffer/Uint8Array/Blob) to
   * {manifest, chunks:[{index,bits,sampleRate,a,b}], a, b}.
   */
  async function restorePackage(input) {
    var buffer = input;
    if (typeof Blob !== "undefined" && input instanceof Blob) {
      buffer = await input.arrayBuffer();
    }
    var files = await unzip(buffer);
    var manifestRaw = files.get("manifest.json");
    if (!manifestRaw) throw new Error("ZIP 中没有 manifest.json");
    var manifest = JSON.parse(new TextDecoder("utf-8").decode(manifestRaw).trim());

    var metas = (manifest.chunks || []).slice().sort(function (x, y) { return x.i - y.i; });
    var chunks = [];
    var partsA = [];
    var partsB = [];
    var total = 0;

    for (var k = 0; k < metas.length; k++) {
      var meta = metas[k];
      var idx = meta.i;
      var stem = "chunk_" + String(idx).padStart(3, "0") + ".wav";
      var wa = files.get("A/" + stem);
      var wb = files.get("B/" + stem);
      if (!wa || !wb) throw new Error("ZIP 缺少第 " + idx + " 块 A/B WAV");
      var ra = parseWav(wa);
      var rb = parseWav(wb);
      if (ra.bits !== rb.bits) throw new Error("第 " + idx + " 块 A/B 位深不一致");
      chunks.push({
        index: idx,
        bits: ra.bits,
        sampleRate: ra.sampleRate,
        samples: ra.samples.length,
        a: ra.samples,
        b: rb.samples
      });
      partsA.push(ra.samples);
      partsB.push(rb.samples);
      total += ra.samples.length;
    }

    var a = concatInt16(partsA, manifest.total_samples || total);
    var b = concatInt16(partsB, manifest.total_samples || total);
    return { manifest: manifest, chunks: chunks, a: a, b: b };
  }

  return {
    supported: supported,
    unzip: unzip,
    parseWav: parseWav,
    encodePcm16Wav: encodePcm16Wav,
    restorePackage: restorePackage
  };
});
