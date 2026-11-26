/*
 * ZIP packaging panel for the segment page.
 *
 * Flow:
 *   1. user fills a whole-ZIP byte cap and clicks "生成打包";
 *   2. POST /api/workspaces/<wid>/package with the current circled segments;
 *   3. the exact stored ZIP is fetched, restored in-browser via ZipRestore
 *      and wired to A/B + per-chunk players and the download button;
 *   4. chunk "保真块" checkboxes drive POST .../package/reallocate using the
 *      *original* cap; on 409 the server keeps the old package and marks and
 *      the whole panel is re-synced from the retained record.
 */
(function () {
  "use strict";

  var WID = window.__WORKSPACE_ID__;
  if (!WID) return;

  var state = {
    pkg: null,
    zipUrl: null,
    urlA: null,
    urlB: null,
    chunkUrls: [],
    restored: null
  };

  var els = {
    budget: document.getElementById("pkg-budget"),
    generate: document.getElementById("pkg-generate"),
    alert: document.getElementById("pkg-alert"),
    panel: document.getElementById("pkg-panel"),
    summary: document.getElementById("pkg-summary"),
    barFill: document.getElementById("pkg-bar-fill"),
    barText: document.getElementById("pkg-bar-text"),
    download: document.getElementById("pkg-download"),
    dlSize: document.getElementById("pkg-dl-size"),
    realloc: document.getElementById("pkg-realloc"),
    budgetHint: document.getElementById("pkg-budget-hint"),
    reallocAlert: document.getElementById("pkg-realloc-alert"),
    chunks: document.getElementById("pkg-chunk-list"),
    audioA: document.getElementById("pkg-audio-a"),
    audioB: document.getElementById("pkg-audio-b"),
    restoreNote: document.getElementById("pkg-restore-note"),
    presets: document.getElementById("pkg-presets")
  };
  if (!els.generate) return;

  // ------------------------------------------------------------ utilities
  function fmtBytes(n) {
    n = Number(n) || 0;
    if (n >= 1024 * 1024) return (n / 1024 / 1024).toFixed(2) + " MB";
    if (n >= 1024) return (n / 1024).toFixed(1) + " KB";
    return n + " B";
  }
  function fmtInt(n) { return (Number(n) || 0).toLocaleString("zh-CN"); }
  function fmtSec(s) { return (Math.round(s * 1000) / 1000).toFixed(2) + "s"; }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function showAlert(which, kind, msg) {
    var box = which === "realloc" ? els.reallocAlert : els.alert;
    if (!box) return;
    if (!msg) { box.hidden = true; box.textContent = ""; return; }
    box.className = "alert " + kind;
    box.textContent = msg;
    box.hidden = false;
  }

  function api(method, path, body) {
    var opts = {
      method: method,
      headers: { "Accept": "application/json" }
    };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    return fetch(path, opts).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) {
        return { status: r.status, ok: r.ok, body: d };
      });
    });
  }

  function currentSegments() {
    if (window.SegmentsPage && typeof window.SegmentsPage.getSegments === "function") {
      return window.SegmentsPage.getSegments();
    }
    return [];
  }

  function revokeUrls() {
    if (state.zipUrl) URL.revokeObjectURL(state.zipUrl);
    if (state.urlA) URL.revokeObjectURL(state.urlA);
    if (state.urlB) URL.revokeObjectURL(state.urlB);
    state.chunkUrls.forEach(function (u) { if (u) URL.revokeObjectURL(u); });
    state.zipUrl = state.urlA = state.urlB = null;
    state.chunkUrls = [];
  }

  function wavBlobUrl(samples, sampleRate) {
    var bytes = window.ZipRestore.encodePcm16Wav(samples, sampleRate || 16000);
    return URL.createObjectURL(new Blob([bytes], { type: "audio/wav" }));
  }

  // ------------------------------------------------------------ rendering
  function renderSummary(pkg) {
    var pct = Math.min(100, (pkg.zip_bytes / Math.max(1, pkg.budget_bytes)) * 100);
    var slack = pkg.budget_bytes - pkg.zip_bytes;
    var n16 = pkg.pcm16_chunk_indices.length;
    var n8 = pkg.pcm8_chunk_indices.length;
    if (els.barFill) els.barFill.style.width = pct.toFixed(1) + "%";
    if (els.barText) {
      els.barText.textContent =
        "ZIP " + fmtInt(pkg.zip_bytes) + " B（" + fmtBytes(pkg.zip_bytes) + "）" +
        " / 上限 " + fmtInt(pkg.budget_bytes) + " B · 剩余 " + fmtInt(slack) + " B";
    }
    if (els.summary) {
      els.summary.innerHTML =
        '<span class="pill ok">PCM16 × ' + n16 + "</span> " +
        '<span class="pill muted">PCM8 × ' + n8 + "</span> " +
        '<span class="pill open">共 ' + pkg.chunk_count + " 块</span>" +
        '<span class="small muted">还原总平方误差 SSE = <strong class="mono">' +
          fmtInt(pkg.total_sse) + "</strong></span>" +
        '<span class="small muted">最小可行 ZIP ≈ ' + fmtInt(pkg.minimum_bytes) + " B</span>";
    }
    if (els.dlSize) els.dlSize.textContent = fmtBytes(pkg.zip_bytes);
    if (els.budgetHint) {
      els.budgetHint.textContent =
        "重新分配始终使用本次上限 " + fmtInt(pkg.budget_bytes) +
        " 字节；与圈选保真段相交的块强制 PCM16。";
    }
  }

  function renderChunks(pkg) {
    if (!els.chunks) return;
    els.chunks.innerHTML = "";
    pkg.chunks.forEach(function (row) {
      var isInter = !!row.intersection_forced;
      var isManual = !!row.manual_fidelity;
      var forced = !!row.forced;

      var item = document.createElement("div");
      item.className = "pkg-chunk" + (row.bit_depth === 16 ? " is16" : " is8");
      item.setAttribute("data-index", String(row.index));

      var checkAttrs = '<input type="checkbox" class="pkg-check" data-index="' + row.index + '"' +
        (forced ? " checked" : "") +
        (isInter ? " disabled title='与圈选保真段相交，必须 PCM16'" :
          ' title="试听后若需要保真，勾选并重新分配"') + ">";

      var badges = "";
      if (isInter) badges += '<span class="pill open">圈选保真</span>';
      if (isManual) badges += '<span class="pill ok">手动保真</span>';

      var depthPill = row.bit_depth === 16
        ? '<span class="pill ok">PCM16</span>'
        : '<span class="pill muted">PCM8</span>';

      var costHint = row.bit_depth === 8 && row.extra_bytes_to_16 > 0
        ? '<span class="small muted">升 PCM16 约 +' + fmtInt(row.extra_bytes_to_16) + " B</span>"
        : (row.bit_depth === 8 ? '<span class="small muted">PCM8（高 8 位）</span>' : "");

      item.innerHTML =
        '<label class="pkg-check-wrap">' + checkAttrs + "</label>" +
        '<div class="pkg-chunk-main">' +
          '<div class="pkg-chunk-line1">' +
            '<span class="pkg-chunk-no">#' + row.index + "</span>" +
            '<span class="mono small">' + fmtSec(row.start) + "–" + fmtSec(row.end) + "</span>" +
            depthPill + badges +
          "</div>" +
          '<div class="pkg-chunk-line2">' + costHint +
            '<span class="small muted">SSE ' + fmtInt(row.sse_pair) + "</span>" +
          "</div>" +
        "</div>" +
        '<audio class="pkg-chunk-audio" controls preload="none" data-chunk="' + row.index + '"></audio>';

      els.chunks.appendChild(item);
      var audio = item.querySelector("audio");
      var url = state.chunkUrls[row.index];
      if (url) audio.src = url;
    });
  }

  // Fetch the exact stored ZIP, restore A/B in-browser, wire all players.
  function loadZipAndRestore(pkg) {
    revokeUrls();
    state.chunkUrls = new Array(pkg.chunk_count);
    if (els.restoreNote) {
      els.restoreNote.className = "small muted";
      els.restoreNote.textContent = "正在从 ZIP 解包并还原 A/B…";
    }

    // Cache-bust only intermediate caches; the endpoint itself is no-store.
    return fetch(pkg.download_url + "?v=" + pkg.version, { headers: { "Accept": "application/zip" } })
      .then(function (r) {
        if (!r.ok) throw new Error("下载 ZIP 失败（" + r.status + "）");
        return r.blob();
      })
      .then(function (blob) {
        if (blob.size !== pkg.zip_bytes) {
          // Not fatal (query/proxy rewriting is unexpected); warn in console.
          console.warn("ZIP size mismatch", blob.size, pkg.zip_bytes);
        }
        state.zipBlob = blob;
        state.zipUrl = URL.createObjectURL(blob);
        els.download.href = state.zipUrl;
        els.download.setAttribute("download",
          "abx-package-" + WID + "-v" + pkg.version + ".zip");

        if (!window.ZipRestore || !window.ZipRestore.supported()) {
          throw new Error("unsupported");
        }
        return window.ZipRestore.restorePackage(blob);
      })
      .then(function (restored) {
        state.restored = restored;
        var sr = (restored.manifest && restored.manifest.sample_rate) || 16000;
        state.urlA = wavBlobUrl(restored.a, sr);
        state.urlB = wavBlobUrl(restored.b, sr);
        els.audioA.src = state.urlA;
        els.audioB.src = state.urlB;

        restored.chunks.forEach(function (c) {
          state.chunkUrls[c.index] = wavBlobUrl(c.a, c.sampleRate || sr);
        });
        renderChunks(state.pkg);

        if (els.restoreNote) {
          els.restoreNote.className = "small";
          els.restoreNote.style.color = "var(--ok)";
          els.restoreNote.textContent =
            "已在网页内从同一份 ZIP 还原 A、B（PCM8 块按 (u8−128)×256 补回 PCM16），" +
            "可直接试听或逐块试听标记。";
        }
      })
      .catch(function (err) {
        // Fallback: server unpacks the *same stored ZIP* to PCM16 WAV.
        var unsupported = String(err && err.message) === "unsupported";
        if (els.audioA) els.audioA.src = pkg.restored_audio_urls.A + "?v=" + pkg.version;
        if (els.audioB) els.audioB.src = pkg.restored_audio_urls.B + "?v=" + pkg.version;
        renderChunks(pkg); // chunk object URLs unavailable; rows render without
        if (els.restoreNote) {
          els.restoreNote.className = "alert warn";
          els.restoreNote.style.margin = "8px 0 0";
          els.restoreNote.textContent = unsupported
            ? "此浏览器不支持网页内 DEFLATE 还原，已改用服务端把同一份 ZIP 解包成 WAV 供试听；ZIP 下载仍是同一份包。"
            : "网页内还原失败（" + (err && err.message) + "），已改用服务端解包试听。";
        }
      });
  }

  function applyPackage(pkg) {
    // Drop the previous package's blob URLs before re-rendering rows so no
    // chunk <audio> briefly points at a URL we are about to revoke.
    revokeUrls();
    state.chunkUrls = [];
    state.pkg = pkg;
    if (els.budget && document.activeElement !== els.budget) {
      els.budget.value = pkg.budget_bytes;
    }
    els.panel.hidden = false;
    renderSummary(pkg);
    renderChunks(pkg); // metadata immediately; audio src attaches after restore
    return loadZipAndRestore(pkg);
  }

  function setBusy(btn, busy, busyText) {
    if (!btn) return;
    if (busy) {
      btn.dataset.label = btn.textContent;
      btn.disabled = true;
      btn.textContent = busyText || "处理中…";
    } else {
      btn.disabled = false;
      if (btn.dataset.label) btn.textContent = btn.dataset.label;
    }
  }

  // ------------------------------------------------------------ generate
  function generate() {
    var raw = (els.budget.value || "").trim();
    var budget = parseInt(raw, 10);
    if (!isFinite(budget) || budget <= 0) {
      showAlert("gen", "error", "请填写正整数的 ZIP 字节上限（单位：字节）。");
      return;
    }
    showAlert("gen", "");
    showAlert("realloc", "");
    setBusy(els.generate, true, "正在分块 / 压缩 / 求最优位深…");

    api("POST", "/api/workspaces/" + WID + "/package", {
      budget_bytes: budget,
      segments: currentSegments(),
      // Preserve any manual marks currently shown/checked; on a first
      // generation there are no checkboxes yet and this evaluates to [].
      manual_chunks: collectManualChunks()
    })
      .then(function (res) {
        if (res.status === 409 && res.body && res.body.code === "package_does_not_fit") {
          showAlert("gen", "error", res.body.error || "按该上限放不下。");
          if (res.body.package) {
            return applyPackage(res.body.package).then(function () {
              showAlert("realloc", "warn", "新上限方案放不下，页面保留上一份包及其保真标记。");
            });
          }
          return null;
        }
        if (!res.ok || !res.body || !res.body.package) {
          throw new Error((res.body && res.body.error) || "打包失败（" + res.status + "）");
        }
        return applyPackage(res.body.package).then(function () {
          showAlert("gen", "ok",
            "打包完成：ZIP " + fmtInt(res.body.package.zip_bytes) +
            " 字节，在上限 " + fmtInt(res.body.package.budget_bytes) + " 内；位深已按 SSE 总和最小分配。");
        });
      })
      .catch(function (err) {
        showAlert("gen", "error", err.message);
      })
      .then(function () { setBusy(els.generate, false); });
  }

  // ---------------------------------------------------------- reallocate
  function collectManualChunks() {
    var out = [];
    if (!els.chunks) return out;
    var boxes = els.chunks.querySelectorAll("input.pkg-check:checked:not(:disabled)");
    Array.prototype.forEach.call(boxes, function (cb) {
      out.push(parseInt(cb.getAttribute("data-index"), 10));
    });
    return out.sort(function (x, y) { return x - y; });
  }

  function reallocate() {
    if (!state.pkg) return;
    var manual = collectManualChunks();
    showAlert("realloc", "");
    setBusy(els.realloc, true, "正在按原上限重新分配…");

    api("POST", "/api/workspaces/" + WID + "/package/reallocate", {
      manual_chunks: manual
      // budget omitted on purpose: server reuses the package's original cap.
    })
      .then(function (res) {
        if (res.status === 409 && res.body && res.body.code === "package_does_not_fit") {
          var msg = res.body.error || "放不下这些保真块。";
          if (res.body.package) {
            // Re-sync marks/audio/download to the retained previous package.
            return applyPackage(res.body.package).then(function () {
              showAlert("realloc", "error",
                msg + " 已保留并恢复显示上一份包及其保真标记。");
            });
          }
          showAlert("realloc", "error", msg);
          return null;
        }
        if (!res.ok || !res.body || !res.body.package) {
          throw new Error((res.body && res.body.error) || "重新分配失败（" + res.status + "）");
        }
        return applyPackage(res.body.package).then(function () {
          showAlert("realloc", "ok",
            "新包已生成（v" + res.body.package.version + "，" +
            fmtInt(res.body.package.zip_bytes) + " B）：保真标记、试听与下载已一起更新。");
        });
      })
      .catch(function (err) {
        showAlert("realloc", "error", err.message);
      })
      .then(function () { setBusy(els.realloc, false); });
  }

  // ------------------------------------------------------------- wiring
  if (els.generate) els.generate.addEventListener("click", generate);
  if (els.realloc) els.realloc.addEventListener("click", reallocate);
  if (els.presets) {
    els.presets.addEventListener("click", function (ev) {
      var btn = ev.target.closest ? ev.target.closest("[data-bytes]") : null;
      if (!btn) return;
      els.budget.value = btn.getAttribute("data-bytes");
      els.budget.focus();
    });
  }

  // Restore the current package on page refresh.
  api("GET", "/api/workspaces/" + WID + "/package").then(function (res) {
    if (res.ok && res.body && res.body.package) applyPackage(res.body.package);
  }).catch(function () { /* no package yet */ });
})();
