// Upload page interactions: file labels, drag & drop, fetch-based submit.
(function () {
  "use strict";

  var form = document.getElementById("upload-form");
  var alertBox = document.getElementById("upload-alert");
  var submitBtn = document.getElementById("upload-btn");

  if (window.__SERVER_UPLOAD_ERROR__) {
    showError(window.__SERVER_UPLOAD_ERROR__);
  }

  function showError(msg) {
    alertBox.textContent = msg;
    alertBox.hidden = false;
  }

  function setLabel(input, labelId, dropId) {
    var label = document.getElementById(labelId);
    var drop = document.getElementById(dropId);
    if (input.files && input.files[0]) {
      label.textContent = input.files[0].name;
      drop.classList.add("has-file");
    } else {
      label.textContent = labelId === "fname-a"
        ? "点击选择或拖入 A 版 WAV"
        : "点击选择或拖入 B 版 WAV";
      drop.classList.remove("has-file");
    }
  }

  var inputA = document.getElementById("file-a");
  var inputB = document.getElementById("file-b");
  inputA.addEventListener("change", function () { setLabel(inputA, "fname-a", "drop-a"); });
  inputB.addEventListener("change", function () { setLabel(inputB, "fname-b", "drop-b"); });

  // Drag & drop support (assign to the matching input).
  [
    { drop: "drop-a", input: inputA, label: "fname-a" },
    { drop: "drop-b", input: inputB, label: "fname-b" }
  ].forEach(function (cfg) {
    var el = document.getElementById(cfg.drop);
    ["dragenter", "dragover"].forEach(function (ev) {
      el.addEventListener(ev, function (e) {
        e.preventDefault();
        e.stopPropagation();
        el.classList.add("drag");
      });
    });
    ["dragleave", "drop"].forEach(function (ev) {
      el.addEventListener(ev, function (e) {
        e.preventDefault();
        e.stopPropagation();
        el.classList.remove("drag");
      });
    });
    el.addEventListener("drop", function (e) {
      var files = e.dataTransfer && e.dataTransfer.files;
      if (files && files.length) {
        cfg.input.files = files;
        setLabel(cfg.input, cfg.label, cfg.drop);
      }
    });
  });

  form.addEventListener("submit", function (e) {
    // Progressive enhancement: if fetch/FormData unavailable, normal POST.
    if (!window.fetch || !window.FormData) return;
    e.preventDefault();
    alertBox.hidden = true;

    if (!inputA.files.length || !inputB.files.length) {
      showError("请先分别选择 A 版和 B 版 WAV 文件。");
      return;
    }

    var fd = new FormData(form);
    submitBtn.disabled = true;
    var oldHtml = submitBtn.innerHTML;
    submitBtn.innerHTML = '<span class="spinner"></span> 正在上传校验…';

    fetch("/api/workspaces", {
      method: "POST",
      body: fd,
      headers: { "Accept": "application/json" }
    })
      .then(function (resp) {
        return resp.json().then(function (data) {
          return { ok: resp.ok, data: data };
        });
      })
      .then(function (r) {
        if (r.ok && r.data && r.data.redirect) {
          window.location.assign(r.data.redirect);
        } else {
          showError((r.data && r.data.error) || "上传失败，请检查文件后重试。");
          submitBtn.disabled = false;
          submitBtn.innerHTML = oldHtml;
        }
      })
      .catch(function (err) {
        showError("网络错误：" + err.message);
        submitBtn.disabled = false;
        submitBtn.innerHTML = oldHtml;
      });
  });

  // Sample form: show busy state but let it do a normal 303 POST.
  var sampleForm = document.getElementById("sample-form");
  if (sampleForm) {
    sampleForm.addEventListener("submit", function () {
      var btn = sampleForm.querySelector("button");
      btn.disabled = true;
      btn.textContent = "正在载入样音…";
    });
  }
})();
