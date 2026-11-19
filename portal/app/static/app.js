(function () {
  "use strict";

  function fallbackCopy(text) {
    var area = document.createElement("textarea");
    area.className = "copy-helper";
    area.value = text;
    area.setAttribute("readonly", "");
    document.body.appendChild(area);
    area.select();
    try {
      document.execCommand("copy");
    } finally {
      document.body.removeChild(area);
    }
    return Promise.resolve();
  }

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).catch(function () {
        return fallbackCopy(text);
      });
    }
    return fallbackCopy(text);
  }

  document.querySelectorAll("[data-copy]").forEach(function (button) {
    button.addEventListener("click", function () {
      var target = document.querySelector(button.getAttribute("data-copy"));
      if (!target) return;
      var value = typeof target.value === "string" ? target.value : target.textContent;
      copyText(value).then(function () {
        var original = button.textContent;
        button.textContent = "Скопировано";
        button.disabled = true;
        if (target.select) target.select();
        window.setTimeout(function () {
          button.textContent = original;
          button.disabled = false;
        }, 1600);
      });
    });
  });

  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      var message = form.getAttribute("data-confirm");
      if (message && !window.confirm(message)) event.preventDefault();
    });
  });

  var revealed = document.getElementById("revealed-key");
  if (revealed) {
    revealed.addEventListener("focus", function () { revealed.select(); });
    if (revealed.value) revealed.select();
  }
})();
