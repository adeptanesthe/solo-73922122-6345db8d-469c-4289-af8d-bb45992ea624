/* ABX question page.
 *
 * Exactly ONE <audio> element is reused for A / B / X.  Before switching
 * sources we remember the playback position (and play state); after the new
 * metadata is loaded we seek to that same position and resume playback.
 * The answer is never present in page data until the judgment is submitted.
 */
(function () {
  "use strict";

  var QID = window.__QID__;
  var player = document.getElementById("player");
  var root = document.getElementById("question-root");

  var q = null;
  var currentKind = "X";
  var position = 0; // last known playback position, seconds
  var loopIndex = null;

  var loadingAlert = document.getElementById("loading-alert");
  var loadError = document.getElementById("load-error");
  var panel = document.getElementById("abx-panel");
  var meta = document.getElementById("q-meta");
  var npText = document.getElementById("np-text");
  var nowPlaying = document.getElementById("now-playing");
  var segJump = document.getElementById("seg-jump");
  var answerForm = document.getElementById("answer-form");
  var answerError = document.getElementById("answer-error");
  var submitBtn = document.getElementById("submit-answer-btn");
  var resultBox = document.getElementById("result");
  var reselectLink = document.getElementById("reselect-link");

  function fmt(x, n) {
    if (x === null || x === undefined || !isFinite(x)) return "—";
    return Number(x).toFixed(n == null ? 3 : n);
  }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function safePlay() {
    try {
      var p = player.play();
      if (p && typeof p.catch === "function") p.catch(function () {
        /* autoplay gesture edge; the native play button still works */
      });
    } catch (_e) { /* media playback unavailable in this environment */ }
  }

  // ------------------------------------------------------------ load question
  fetch("/api/questions/" + QID, { headers: { Accept: "application/json" } })
    .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
    .then(function (res) {
      if (!res.ok || !res.d.ok) throw new Error((res.d && res.d.error) || "题目载入失败");
      q = res.d.question;
      initQuestion();
    })
    .catch(function (err) {
      loadingAlert.hidden = true;
      loadError.hidden = false;
      loadError.textContent = err.message + "。请从题目记录页重新进入。";
    });

  function initQuestion() {
    loadingAlert.hidden = true;
    panel.hidden = false;

    var segs = q.segments || [];
    meta.textContent =
      fmt(q.duration, 2) + " 秒 · 增益 g=" + fmt(q.gain, 5) +
      " · " + segs.length + " 个对比段 · 开题于 " +
      new Date(q.created_at * 1000).toLocaleString();

    if (q.workspace_id) {
      reselectLink.href = "/workspaces/" + q.workspace_id + "/segments";
      reselectLink.textContent = "改选对比段再开新题 →";
    }

    renderSegmentChips();
    renderCalibMini();

    // Initial source: X (opaque URL, does not reveal identity).
    player.src = q.audio_urls.X;
    setActiveButton("X");

    player.addEventListener("timeupdate", function () {
      position = player.currentTime;
      maybeLoop();
    });
    player.addEventListener("play", function () { nowPlaying.classList.add("playing"); });
    player.addEventListener("pause", function () { nowPlaying.classList.remove("playing"); });
    player.addEventListener("ended", function () { nowPlaying.classList.remove("playing"); });

    document.querySelectorAll(".src-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        switchKind(btn.getAttribute("data-kind"));
      });
    });

    document.querySelectorAll('input[name=choice]').forEach(function (radio) {
      radio.addEventListener("change", function () {
        document.getElementById("choice-a").classList.toggle("checked", radio.value === "A" && radio.checked);
        document.getElementById("choice-b").classList.toggle("checked", radio.value === "B" && radio.checked);
      });
    });

    answerForm.addEventListener("submit", onSubmit);

    document.addEventListener("keydown", function (e) {
      var tag = (e.target.tagName || "").toLowerCase();
      if (["input", "textarea", "select", "button", "a"].indexOf(tag) !== -1) return;
      if (e.key === "a" || e.key === "A") switchKind("A");
      else if (e.key === "b" || e.key === "B") switchKind("B");
      else if (e.key === "x" || e.key === "X") switchKind("X");
      else if (e.code === "Space") {
        e.preventDefault();
        if (player.paused) player.play().catch(function () {});
        else player.pause();
      }
    });

    if (q.status === "answered" && q.answer) {
      renderAnswered(q.answer, true);
    }
  }

  // ------------------------------------------------- source switching (ABX)
  function setActiveButton(kind) {
    currentKind = kind;
    document.querySelectorAll(".src-btn").forEach(function (btn) {
      var active = btn.getAttribute("data-kind") === kind;
      btn.classList.toggle("active", active);
      btn.setAttribute("aria-pressed", active ? "true" : "false");
    });
    npText.textContent = "当前：" + kind;
  }

  function switchKind(kind) {
    if (!q || kind === currentKind) {
      // Even if same source, give keyboard/click feedback; do not restart.
      return;
    }

    // Preserve position + play state from the outgoing source.
    var pos = (player.readyState >= 1 && isFinite(player.currentTime))
      ? player.currentTime
      : position;
    if (player.duration && pos >= player.duration - 0.05) {
      pos = 0; // had reached the end -> start the alternative from the top
    }
    var wasPlaying = !player.paused && !player.ended;
    position = pos;

    setActiveButton(kind);
    npText.textContent = "切换中：" + kind + " …";

    player.onloadedmetadata = function () {
      try {
        player.currentTime = Math.min(pos, player.duration || pos);
      } catch (_e) { /* seek before metadata fully ready; ignored */ }
      if (wasPlaying) {
        safePlay();
      }
      npText.textContent = "当前：" + kind;
    };
    player.src = q.audio_urls[kind];
    player.load();
  }

  // ------------------------------------------------------- segment jumping
  function renderSegmentChips() {
    var segs = q.segments || [];
    var label = segJump.querySelector(".label");
    segJump.innerHTML = "";
    segJump.appendChild(label || makeLabel());

    segs.forEach(function (seg, i) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "seg-chip";
      btn.setAttribute("data-i", String(i));
      btn.title = "跳转到第 " + (i + 1) + " 段并在段内循环；再次点击取消循环";
      btn.textContent = "第" + (i + 1) + "段 " + seg.start.toFixed(2) + "–" + seg.end.toFixed(2);
      btn.addEventListener("click", function () {
        if (loopIndex === i) {
          loopIndex = null;
          btn.classList.remove("loop-on");
        } else {
          loopIndex = i;
          document.querySelectorAll(".seg-chip").forEach(function (c) { c.classList.remove("loop-on"); });
          btn.classList.add("loop-on");
          try { player.currentTime = seg.start; } catch (_e) {}
          position = seg.start;
          if (player.paused) safePlay();
        }
      });
      segJump.appendChild(btn);
    });
  }

  function makeLabel() {
    var span = document.createElement("span");
    span.className = "label";
    span.textContent = "对比段：";
    return span;
  }

  function maybeLoop() {
    if (loopIndex === null || !q) return;
    var seg = q.segments[loopIndex];
    if (!seg) return;
    if (player.currentTime >= seg.end - 0.02) {
      player.currentTime = seg.start + 0.001;
    }
  }

  // --------------------------------------------------------- calibration UI
  function renderCalibMini() {
    var card = document.getElementById("calib-info-card");
    var box = document.getElementById("calib-mini");
    card.hidden = false;
    var c = q.calibration || {};
    var html =
      '<div class="kv">' +
      "<dt>采用增益 g</dt><dd>" + fmt(q.gain, 6) +
        (q.gain_db != null ? "（" + (q.gain_db >= 0 ? "+" : "") + fmt(q.gain_db, 3) + " dB）" : "") + "</dd>" +
      "<dt>RMS 可行区间</dt><dd>[" + fmt(c.rms_gain_lower, 6) + ", " +
        fmt(c.effective_gain_upper != null ? c.effective_gain_upper : c.rms_gain_upper, 6) + "]</dd>" +
      "<dt>削波上限</dt><dd>" + (c.clip_gain_limit != null ? fmt(c.clip_gain_limit, 6) : "∞") +
        "（B 峰值 " + c.peak_abs_b + "）</dd>" +
      "</div>" +
      '<table class="data"><thead><tr><th>段</th><th>区间(s)</th>' +
      "<th>校准后 RMS B/A</th><th>是否在 0.95–1.05</th></tr></thead><tbody>";
    (q.segments || []).forEach(function (row) {
      var r = row.ratio_calibrated;
      var ok = r != null && r >= 0.95 - 1e-9 && r <= 1.05 + 1e-9;
      html += "<tr><td>第" + row.index + '段</td>' +
        '<td class="mono">' + row.start.toFixed(2) + "–" + row.end.toFixed(2) + "</td>" +
        '<td class="mono">' + fmt(r, 5) + "</td>" +
        "<td>" + (ok ? '<span class="pill ok">达标</span>' : '<span class="pill bad">异常</span>') +
        "</td></tr>";
    });
    html += "</tbody></table>";
    box.innerHTML = html;
  }

  // ----------------------------------------------------------- answer flow
  function onSubmit(e) {
    e.preventDefault();
    answerError.hidden = true;
    var checked = answerForm.querySelector('input[name=choice]:checked');
    if (!checked) {
      answerError.textContent = "请先选择“X 是 A”或“X 是 B”，再提交。";
      answerError.hidden = false;
      return;
    }
    submitBtn.disabled = true;
    var oldText = submitBtn.textContent;
    submitBtn.innerHTML = '<span class="spinner"></span> 判分中…';

    fetch("/api/questions/" + QID + "/answer", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ choice: checked.value })
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) {
        if (!res.ok) {
          answerError.textContent = (res.d && res.d.error) || "提交失败";
          answerError.hidden = false;
          submitBtn.disabled = false;
          submitBtn.textContent = oldText;
          return;
        }
        // The response is the first moment the answer is allowed to exist
        // on the client.
        renderAnswered(
          { x_is: res.d.x_is, choice: res.d.choice, correct: res.d.correct },
          !!res.d.already_answered
        );
      })
      .catch(function (err) {
        answerError.textContent = "网络错误：" + err.message;
        answerError.hidden = false;
        submitBtn.disabled = false;
        submitBtn.textContent = oldText;
      });
  }

  function renderAnswered(ans, already) {
    // Lock the form.
    answerForm.querySelectorAll("input").forEach(function (i) { i.disabled = true; });
    submitBtn.disabled = true;
    submitBtn.textContent = already ? "该题已提交过判断" : "已提交";
    var radio = answerForm.querySelector('input[value=' + ans.choice + ']');
    if (radio) {
      radio.checked = true;
      document.getElementById("choice-" + ans.choice.toLowerCase()).classList.add("checked");
    }

    // Reveal which source X really was on the switcher button.
    var xSub = document.querySelector('.src-btn[data-kind="X"] .sub');
    if (xSub) xSub.textContent = "答案：X = " + ans.x_is;

    resultBox.hidden = false;
    resultBox.className = "result-banner " + (ans.correct ? "correct" : "wrong");
    var html = ans.correct
      ? "<h2>✓ 判断正确</h2>"
      : "<h2>✗ 判断错误</h2>";
    html += '<div class="answer-reveal">正确答案：<strong class="big">X 就是 ' +
      escapeHtml(ans.x_is) + "</strong><br>" +
      "你的判断：X 是 <strong>" + escapeHtml(ans.choice) + "</strong> · " +
      (ans.correct ? "两版的差异没有骗过你的耳朵。" : "再切回 A/B/X 对比听听吧（音频仍可重播）。") +
      (already ? '<br><span class="muted small">这道题此前已提交，记录保持第一次判分结果。</span>' : "") +
      "</div>" +
      '<div class="actions"><a class="btn secondary" href="/questions">查看题目记录</a>' +
      (q.workspace_id
        ? '<a class="btn" href="/workspaces/' + q.workspace_id + '/segments">改选对比段再开新题 →</a>'
        : "") +
      "</div>";
    resultBox.innerHTML = html;
    resultBox.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
})();
