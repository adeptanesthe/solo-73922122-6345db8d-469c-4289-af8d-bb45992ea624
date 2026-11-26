/* Segment selection + calibration page.
 * Vanilla JS (no external dependencies so it works fully offline).
 */
(function () {
  "use strict";

  var WID = window.__WORKSPACE_ID__;
  var MAX_SEGMENTS = window.__MAX_SEGMENTS__ || 6;

  var state = {
    info: null,
    segments: [], // {start_sample, end_sample}
    calibration: null,
    fresh: false,
    feasible: false,
    draft: null, // {start_sample, end_sample} while dragging
    dragging: false
  };

  var els = {
    meta: document.getElementById("file-meta"),
    canvasA: document.getElementById("canvas-a"),
    canvasB: document.getElementById("canvas-b"),
    axis: document.getElementById("time-axis"),
    suggestions: document.getElementById("suggestions"),
    list: document.getElementById("seg-list"),
    count: document.getElementById("seg-count"),
    addBtn: document.getElementById("add-segment"),
    clearBtn: document.getElementById("clear-segments"),
    calibBtn: document.getElementById("calibrate-btn"),
    openBtn: document.getElementById("open-question-btn"),
    alert: document.getElementById("calib-alert"),
    report: document.getElementById("calib-report")
  };

  var COLORS = {
    aLine: "#38bdf8",
    aFill: "rgba(14,165,233,.35)",
    bLine: "#fbbf24",
    bFill: "rgba(245,158,11,.30)",
    segFill: "rgba(129,140,248,.22)",
    segBorder: "#818cf8",
    draftFill: "rgba(16,185,129,.18)",
    draftBorder: "#34d399",
    grid: "rgba(148,163,184,.16)",
    text: "#93a4c3"
  };

  // ------------------------------------------------------------------ utils
  function fmtSec(s) { return (Math.round(s * 1000) / 1000).toFixed(2) + "s"; }
  function fmtNum(x, digits) {
    if (x === null || x === undefined || !isFinite(x)) return "—";
    return Number(x).toFixed(digits == null ? 4 : digits);
  }
  function sampleToSec(s) { return s / 16000; }
  function secToSample(v) { return Math.round(parseFloat(v) * 16000); }

  function showAlert(kind, msg) {
    els.alert.className = "alert " + kind;
    els.alert.textContent = msg;
    els.alert.hidden = false;
  }
  function hideAlert() { els.alert.hidden = true; }

  function markStale() {
    state.fresh = false;
    state.feasible = false;
    updateOpenState();
  }

  // The button stays clickable as long as >=1 segment exists; the click
  // handler ensures a fresh calibration and the server hard-enforces the
  // feasible-only rule.  This avoids a disabled-button race for fast inputs.
  function updateOpenState(busy) {
    if (busy) {
      els.openBtn.disabled = true;
      return;
    }
    els.openBtn.disabled = !state.segments.length;
  }

  // ------------------------------------------------------------- data load
  fetch("/api/workspaces/" + WID, { headers: { "Accept": "application/json" } })
    .then(function (r) {
      if (!r.ok) throw new Error("无法读取工作区信息（" + r.status + "）");
      return r.json();
    })
    .then(function (data) {
      state.info = data;
      els.meta.innerHTML =
        "<strong>" + escapeHtml(data.filename_a || "A.wav") + "</strong> 对 " +
        "<strong>" + escapeHtml(data.filename_b || "B.wav") + "</strong>" +
        " · " + fmtSec(data.duration) +
        " · " + data.sample_rate + " Hz · mono · PCM16 · " +
        data.samples.toLocaleString() + " samples";
      buildTimeAxis(data.duration);
      buildSuggestions(data.suggestions || []);
      resizeCanvases();
      // Convenient starting selection: first two suggested ranges (or one).
      var pre = (data.suggestions || []).slice(0, Math.min(2, MAX_SEGMENTS));
      pre.forEach(function (s) {
        state.segments.push({
          start_sample: s.start_sample,
          end_sample: s.end_sample
        });
      });
      renderList();
      drawAll();
      scheduleCalibrate(350);
    })
    .catch(function (err) {
      els.meta.textContent = "";
      showAlert("error", err.message + "。请返回首页重新上传。");
    });

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // ------------------------------------------------------------ time axis
  function buildTimeAxis(duration) {
    var nTicks = 10;
    var html = "";
    for (var i = 0; i <= nTicks; i++) {
      var t = (duration * i) / nTicks;
      var pct = (i / nTicks) * 100;
      html += '<span class="time-tick" style="left:' + pct + '%">' + t.toFixed(1) + "</span>";
    }
    els.axis.innerHTML = html;
  }

  // ---------------------------------------------------------- suggestions
  function buildSuggestions(sugs) {
    if (!sugs.length) {
      els.suggestions.innerHTML =
        '<span class="small muted">没有自动检测到两版均非静音的连续区间，请直接在波形上圈选。</span>';
      return;
    }
    var html = '<span class="label small muted" style="align-self:center">建议区间（点击加入）：</span>';
    sugs.slice(0, 8).forEach(function (s, i) {
      html += '<button type="button" class="chip" data-i="' + i + '">' +
        "#" + (i + 1) + " " + s.start.toFixed(2) + "–" + s.end.toFixed(2) + "s</button>";
    });
    els.suggestions.innerHTML = html;
    Array.prototype.forEach.call(els.suggestions.querySelectorAll(".chip"), function (btn) {
      btn.addEventListener("click", function () {
        var s = sugs[parseInt(btn.getAttribute("data-i"), 10)];
        if (state.segments.length >= MAX_SEGMENTS) {
          showAlert("warn", "最多只能选择 " + MAX_SEGMENTS + " 段。");
          return;
        }
        if (isDuplicate(s.start_sample, s.end_sample)) return;
        state.segments.push({ start_sample: s.start_sample, end_sample: s.end_sample });
        hideAlert();
        renderList();
        drawAll();
        markStale();
        scheduleCalibrate(150);
      });
    });
  }

  function isDuplicate(s, e) {
    return state.segments.some(function (g) {
      return Math.abs(g.start_sample - s) < 160 && Math.abs(g.end_sample - e) < 160;
    });
  }

  // -------------------------------------------------------------- canvases
  function resizeCanvases() {
    [els.canvasA, els.canvasB].forEach(function (cv) {
      var dpr = window.devicePixelRatio || 1;
      var cssW = cv.clientWidth || cv.parentElement.clientWidth;
      var cssH = 110;
      cv.width = Math.round(cssW * dpr);
      cv.height = Math.round(cssH * dpr);
      var ctx = cv.getContext("2d");
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    });
    drawAll();
  }

  var resizeTimer = null;
  window.addEventListener("resize", function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(resizeCanvases, 120);
  });

  function drawAll() {
    if (!state.info) return;
    drawWave(els.canvasA, state.info.peaks_a, COLORS.aLine, COLORS.aFill);
    drawWave(els.canvasB, state.info.peaks_b, COLORS.bLine, COLORS.bFill);
  }

  function roundedRect(ctx, x, y, w, h, r) {
    r = Math.min(r, w / 2, h / 2);
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  function drawWave(cv, peaks, lineColor, fillColor) {
    var ctx = cv.getContext("2d");
    var W = cv.clientWidth, H = 110;
    ctx.clearRect(0, 0, W, H);

    // horizontal center + grid
    ctx.strokeStyle = COLORS.grid;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, H / 2);
    ctx.lineTo(W, H / 2);
    ctx.stroke();

    var mins = peaks.min, maxs = peaks.max, B = peaks.buckets;
    var mid = H / 2, amp = H / 2 - 8;
    var nSamples = state.info.samples;

    // segment overlays (sample -> pixel)
    state.segments.forEach(function (seg, i) {
      var x0 = (seg.start_sample / nSamples) * W;
      var x1 = (seg.end_sample / nSamples) * W;
      ctx.fillStyle = COLORS.segFill;
      ctx.fillRect(x0, 0, Math.max(1, x1 - x0), H);
      ctx.strokeStyle = COLORS.segBorder;
      ctx.lineWidth = 1.5;
      ctx.strokeRect(x0 + 0.5, 0.5, Math.max(1, x1 - x0) - 1, H - 1);

      // Number pill at the bottom-left so it never collides with the
      // A/B badge anchored at the top-left.
      var label = String(i + 1);
      ctx.font = "700 11px ui-monospace, SFMono-Regular, Menlo, monospace";
      var tw = ctx.measureText(label).width;
      var pw = tw + 9, ph = 15, px = x0 + 4, py = H - ph - 5;
      ctx.fillStyle = "rgba(67,56,202,.92)";
      roundedRect(ctx, px, py, pw, ph, 4);
      ctx.fill();
      ctx.fillStyle = "#ffffff";
      ctx.textBaseline = "middle";
      ctx.fillText(label, px + 4.5, py + ph / 2 + 0.5);
      ctx.textBaseline = "alphabetic";
    });

    // draft overlay
    if (state.draft) {
      var dx0 = (state.draft.start_sample / nSamples) * W;
      var dx1 = (state.draft.end_sample / nSamples) * W;
      if (dx1 < dx0) { var t = dx0; dx0 = dx1; dx1 = t; }
      ctx.fillStyle = COLORS.draftFill;
      ctx.fillRect(dx0, 0, Math.max(1, dx1 - dx0), H);
      ctx.strokeStyle = COLORS.draftBorder;
      ctx.setLineDash([4, 3]);
      ctx.strokeRect(dx0 + 0.5, 0.5, Math.max(1, dx1 - dx0) - 1, H - 1);
      ctx.setLineDash([]);
    }

    if (!B) return;
    var bw = W / B;
    ctx.beginPath();
    for (var k = 0; k < B; k++) {
      var x = k * bw;
      var yMax = mid - (maxs[k] / 32768) * amp;
      var yMin = mid - (mins[k] / 32768) * amp;
      ctx.rect(x, yMax, Math.max(bw * 0.8, 0.6), Math.max(1, yMin - yMax));
    }
    ctx.fillStyle = fillColor;
    ctx.fill();

    // center line trace
    ctx.beginPath();
    ctx.strokeStyle = lineColor;
    ctx.lineWidth = 1;
    for (var k2 = 0; k2 < B; k2++) {
      var x2 = k2 * bw + bw / 2;
      var yMax2 = mid - (maxs[k2] / 32768) * amp;
      var yMin2 = mid - (mins[k2] / 32768) * amp;
      ctx.moveTo(x2, yMax2);
      ctx.lineTo(x2, yMin2);
    }
    ctx.globalAlpha = 0.85;
    ctx.stroke();
    ctx.globalAlpha = 1;
  }

  // ------------------------------------------------------- pointer dragging
  function sampleFromEvent(cv, ev) {
    var rect = cv.getBoundingClientRect();
    var x = Math.min(Math.max(ev.clientX - rect.left, 0), rect.width);
    return Math.round((x / rect.width) * state.info.samples);
  }

  [els.canvasA, els.canvasB].forEach(function (cv) {
    cv.addEventListener("pointerdown", function (ev) {
      if (!state.info) return;
      if (state.segments.length >= MAX_SEGMENTS) {
        showAlert("warn", "最多只能圈选 " + MAX_SEGMENTS + " 段；可先删除一段。");
        return;
      }
      cv.setPointerCapture(ev.pointerId);
      state.dragging = true;
      var s = sampleFromEvent(cv, ev);
      state.draft = { start_sample: s, end_sample: s };
    });

    cv.addEventListener("pointermove", function (ev) {
      if (!state.dragging || !state.draft) return;
      state.draft.end_sample = sampleFromEvent(cv, ev);
      drawAll();
    });

    function finish(ev) {
      if (!state.dragging) return;
      state.dragging = false;
      var d = state.draft;
      state.draft = null;
      if (d) {
        var s = Math.min(d.start_sample, d.end_sample);
        var e = Math.max(d.start_sample, d.end_sample);
        var rect = cv.getBoundingClientRect();
        var pxWidth = ((e - s) / state.info.samples) * rect.width;
        if (e > s && pxWidth >= 8) {
          if (state.segments.length < MAX_SEGMENTS) {
            state.segments.push({ start_sample: s, end_sample: e });
            renderList();
            markStale();
            scheduleCalibrate(250);
          }
        }
      }
      drawAll();
    }
    cv.addEventListener("pointerup", finish);
    cv.addEventListener("pointercancel", function () {
      state.dragging = false;
      state.draft = null;
      drawAll();
    });
  });

  // --------------------------------------------------------- segment list
  function renderList() {
    els.list.innerHTML = "";
    state.segments.forEach(function (seg, i) {
      var row = document.createElement("div");
      row.className = "seg-row";
      var dur = sampleToSec(seg.end_sample - seg.start_sample);
      row.innerHTML =
        '<span class="seg-index">' + (i + 1) + "</span>" +
        '<div class="seg-times">' +
          '<input type="number" step="0.01" min="0" aria-label="第' + (i + 1) + '段起点（秒）" data-field="start" value="' + sampleToSec(seg.start_sample).toFixed(3) + '">' +
          "<span>→</span>" +
          '<input type="number" step="0.01" min="0" aria-label="第' + (i + 1) + '段终点（秒）" data-field="end" value="' + sampleToSec(seg.end_sample).toFixed(3) + '">' +
          '<span class="seg-meta" data-role="dur">' + dur.toFixed(2) + " s · " +
            (seg.end_sample - seg.start_sample).toLocaleString() + " 采样</span>" +
        "</div>" +
        '<div class="seg-actions">' +
          '<button type="button" class="btn ghost sm" data-act="play" title="在波形上高亮该段">高亮</button>' +
          '<button type="button" class="btn danger sm" data-act="del">删除</button>' +
        "</div>";

      var startInput = row.querySelector('[data-field=start]');
      var endInput = row.querySelector('[data-field=end]');

      function onTimeChange() {
        var s = secToSample(startInput.value);
        var e = secToSample(endInput.value);
        if (!isFinite(s) || !isFinite(e) || s < 0 || e > state.info.samples || s >= e) {
          showAlert("error", "第 " + (i + 1) + " 段时间无效：需满足 0 ≤ 起点 < 终点 ≤ " +
            fmtSec(state.info.duration) + "。");
          return;
        }
        hideAlert();
        seg.start_sample = s;
        seg.end_sample = e;
        row.querySelector('[data-role=dur]').textContent =
          sampleToSec(e - s).toFixed(2) + " s · " + (e - s).toLocaleString() + " 采样";
        drawAll();
        markStale();
        scheduleCalibrate(400);
      }
      startInput.addEventListener("change", onTimeChange);
      endInput.addEventListener("change", onTimeChange);

      row.querySelector('[data-act=del]').addEventListener("click", function () {
        state.segments.splice(i, 1);
        renderList();
        drawAll();
        markStale();
        if (state.segments.length) scheduleCalibrate(200);
        else { els.report.hidden = true; hideAlert(); }
      });
      row.querySelector('[data-act=play]').addEventListener("click", function () {
        // Jump to the question-opening flow is not available yet; briefly
        // highlight the segment on the waveform instead.
        flashSegment(seg);
      });

      els.list.appendChild(row);
    });

    els.count.textContent = "已选 " + state.segments.length + " / " + MAX_SEGMENTS + " 段";
    els.addBtn.disabled = state.segments.length >= MAX_SEGMENTS;
    updateOpenState();
  }

  function flashSegment(seg) {
    state.draft = { start_sample: seg.start_sample, end_sample: seg.end_sample };
    drawAll();
    setTimeout(function () { state.draft = null; drawAll(); }, 650);
  }

  els.addBtn.addEventListener("click", function () {
    if (!state.info || state.segments.length >= MAX_SEGMENTS) return;
    var dur = state.info.samples;
    // Default: a 2-second block in the middle (or shorter near the end).
    var len = Math.min(32000, dur);
    var center = Math.floor(dur / 2);
    var s = Math.max(0, center - Math.floor(len / 2));
    var e = Math.min(dur, s + len);
    state.segments.push({ start_sample: s, end_sample: e });
    renderList();
    drawAll();
    markStale();
    scheduleCalibrate(200);
  });

  els.clearBtn.addEventListener("click", function () {
    state.segments = [];
    renderList();
    drawAll();
    markStale();
    els.report.hidden = true;
    hideAlert();
  });

  // ---------------------------------------------------------- calibration
  var calibTimer = null;
  function scheduleCalibrate(delay) {
    clearTimeout(calibTimer);
    calibTimer = setTimeout(runCalibration, delay == null ? 200 : delay);
  }

  els.calibBtn.addEventListener("click", runCalibration);

  function payloadSegments() {
    return state.segments.map(function (s) {
      return { start_sample: s.start_sample, end_sample: s.end_sample };
    });
  }

  function runCalibration() {
    if (!state.info) return Promise.resolve(null);
    if (!state.segments.length) {
      markStale();
      els.report.hidden = true;
      showAlert("warn", "请先圈出至少 1 段两版均非静音的区间。");
      return Promise.resolve(null);
    }
    hideAlert();
    els.calibBtn.disabled = true;
    var oldText = els.calibBtn.textContent;
    els.calibBtn.innerHTML = '<span class="spinner dark"></span> 计算中…';

    return fetch("/api/workspaces/" + WID + "/calibrate", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify({ segments: payloadSegments() })
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) {
        if (!res.ok) {
          markStale();
          showAlert("error", (res.d && res.d.error) || "校准请求失败。");
          els.report.hidden = true;
          return null;
        }
        state.calibration = res.d;
        state.fresh = true;
        state.feasible = !!res.d.feasible;
        renderCalibration(res.d);
        return res.d;
      })
      .catch(function (err) {
        markStale();
        showAlert("error", "网络错误：" + err.message);
        return null;
      })
      .finally(function () {
        els.calibBtn.disabled = false;
        els.calibBtn.textContent = oldText;
      });
  }

  function ratioPill(r) {
    if (r === null || r === undefined) return '<span class="pill bad">静音</span>';
    if (r >= 0.95 - 1e-9 && r <= 1.05 + 1e-9)
      return '<span class="pill ok">' + fmtNum(r, 4) + " ✓</span>";
    return '<span class="pill bad">' + fmtNum(r, 4) + " ✗</span>";
  }

  function renderCalibration(rep) {
    els.report.hidden = false;
    var html = "";

    if (rep.feasible) {
      updateOpenState();
      showAlert("ok", "校准有可行解：可以开题。系统会把该校准后的 B 音频冻结到题目中。");
      html +=
        '<div class="gain-hero">' +
          "<div><div class=\"small muted\">全片共用增益 g（作用于 B）</div>" +
          '<div class="gain-value">' + fmtNum(rep.gain, 6) + "</div></div>" +
          '<div class="muted">≈ ' + (rep.gain_db >= 0 ? "+" : "") +
            fmtNum(rep.gain_db, 3) + " dB<br>最接近 1 的可行取值</div>" +
        "</div>";
    } else {
      updateOpenState();
      showAlert("error", "校准无解，不能开新题。" + (rep.reason || ""));
      html +=
        '<div class="gain-hero"><div><div class="small muted">全片共用增益 g</div>' +
        '<div class="gain-value" style="color:var(--bad)">无可行解</div></div></div>';
    }

    html += '<dl class="kv">';
    html += "<dt>RMS 交集下界 g ≥</dt><dd>" + fmtNum(rep.rms_gain_lower, 6) + "</dd>";
    html += "<dt>RMS 交集上界 g ≤</dt><dd>" + fmtNum(rep.rms_gain_upper, 6) + "</dd>";
    if (rep.clip_gain_limit !== null && rep.clip_gain_limit !== undefined) {
      html += "<dt>PCM16 削波上限 g ≤</dt><dd>" + fmtNum(rep.clip_gain_limit, 6) +
        " <span class='muted'>(B 峰值 " + rep.peak_abs_b + ")</span></dd>";
    }
    if (rep.effective_gain_upper !== null && rep.effective_gain_upper !== undefined) {
      html += "<dt>实际上界</dt><dd>" + fmtNum(rep.effective_gain_upper, 6) + "</dd>";
    }
    if (rep.feasible) {
      html += "<dt>校准后最大 |样点|</dt><dd>" + fmtNum(rep.max_scaled_abs, 1) +
        " / 32767（负向边界 -32768）</dd>";
    }
    html += "</dl>";

    html += '<table class="data"><thead><tr>' +
      "<th>段</th><th>区间 (s)</th><th>RMS A</th><th>RMS B 原生</th>" +
      "<th>原生 B/A</th><th>校准后 B/A</th><th>0.95–1.05</th>" +
      "</tr></thead><tbody>";
    rep.segments.forEach(function (row) {
      html += "<tr>" +
        "<td>第 " + row.index + " 段</td>" +
        '<td class="mono">' + row.start.toFixed(2) + "–" + row.end.toFixed(2) + "</td>" +
        '<td class="mono">' + fmtNum(row.rms_a, 1) + "</td>" +
        '<td class="mono">' + fmtNum(row.rms_b, 1) + "</td>" +
        '<td class="mono">' + fmtNum(row.ratio_native, 4) + "</td>" +
        '<td class="mono">' + fmtNum(row.ratio_calibrated, 4) + "</td>" +
        "<td>" + ratioPill(row.ratio_calibrated) + "</td>" +
      "</tr>";
    });
    html += "</tbody></table>";
    els.report.innerHTML = html;
  }

  // --------------------------------------------------------- open question
  els.openBtn.addEventListener("click", function () {
    if (!state.segments.length) {
      showAlert("warn", "请先圈出至少 1 段两版均非静音的区间。");
      return;
    }

    var old = els.openBtn.textContent;
    els.openBtn.disabled = true;
    els.openBtn.innerHTML = '<span class="spinner"></span> 校准 / 开题中…';

    // Always make the click converge: if the current selection has not been
    // calibrated yet (or was edited), calibrate first.  The server performs
    // the same hard feasibility check when the question is created.
    Promise.resolve()
      .then(function () {
        if (state.fresh && state.calibration) return state.calibration;
        return runCalibration();
      })
      .then(function (rep) {
        if (!rep || !rep.feasible) {
          state.feasible = false;
          throw new Error((rep && rep.reason) || "校准未完成或无可行解，暂不能开新题；请查看上方校准结果。");
        }
        return fetch("/api/workspaces/" + WID + "/questions", {
          method: "POST",
          headers: { "Content-Type": "application/json", "Accept": "application/json" },
          body: JSON.stringify({ segments: payloadSegments() })
        }).then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); });
      })
      .then(function (res) {
        // res is either the fetch wrapper {ok,d} from question creation.
        if (res && res.redirect) {
          window.location.assign(res.redirect);
          return new Promise(function () {}); // leave spinner until navigation
        }
        if (res && res.ok && res.d && res.d.redirect) {
          window.location.assign(res.d.redirect);
          return new Promise(function () {});
        }
        if (res && res.d && res.d.code === "infeasible" && res.d.calibration) {
          renderCalibration(res.d.calibration);
          throw new Error(res.d.error || "校准无解，不能开新题。");
        }
        throw new Error((res && res.d && res.d.error) || "开题失败。");
      })
      .catch(function (err) {
        showAlert("error", err.message);
        els.openBtn.disabled = false;
        els.openBtn.textContent = old;
      });
  });
})();
