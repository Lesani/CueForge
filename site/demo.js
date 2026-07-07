// Miniature of the CueForge playing view. Same rules as the real thing:
// GO advances column-major, normal cues cut each other, backgrounds loop
// until a stop cue ends them, played cues turn green.
(function () {
  "use strict";

  var CUES = [
    { name: "Preshow music", type: "bg",   meta: "bg · loop" },
    { name: "House to half", type: "normal", dur: 3000, meta: "0:03" },
    { name: "Overture",      type: "normal", dur: 4000, meta: "0:04" },
    { name: "Doorbell",      type: "normal", dur: 2000, meta: "0:02" },
    { name: "Rain outside",  type: "bg",   meta: "bg · loop" },
    { name: "Stop rain",     type: "stop", target: 4, meta: "stop · fade" },
  ];

  var grid = document.getElementById("demoGrid");
  var goBtn = document.getElementById("demoGo");
  var liveEl = document.getElementById("demoLive");
  var timerEl = document.getElementById("demoTimer");
  if (!grid) return;

  var reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  var cells = CUES.map(function (cue, i) {
    var el = document.createElement("button");
    el.type = "button";
    el.className = "dcell";
    el.dataset.type = cue.type;
    el.innerHTML =
      '<span class="dfill"></span>' +
      '<span class="dname">' + cue.name + "</span>" +
      '<span class="dmeta">' + cue.meta + "</span>";
    el.addEventListener("click", function () {
      autopilot = false;
      fire(i);
    });
    grid.appendChild(el);
    return el;
  });

  var next = 0;              // cursor: index GO fires next
  var playing = -1;          // index of the running normal cue
  var playEnd = 0;           // wall-clock end of the running normal cue
  var raf = 0;
  var autopilot = !reduced;  // demo runs itself until the user touches it
  var autoTimer = 0;

  function fmt(ms) {
    var s = Math.max(0, ms) / 1000;
    var m = Math.floor(s / 60);
    return m + ":" + ("0" + Math.floor(s % 60)).slice(-2) + "." + Math.floor((s % 1) * 10);
  }

  function setCursor() {
    cells.forEach(function (el, i) { el.classList.toggle("cursor", i === next); });
  }

  function clearFill(i) {
    var f = cells[i].querySelector(".dfill");
    f.style.transition = "none";
    f.style.transform = "translateX(-100%)";
  }

  function stopNormal() {
    if (playing < 0) return;
    clearFill(playing);
    cells[playing].classList.add("played");
    playing = -1;
    cancelAnimationFrame(raf);
  }

  function tick() {
    var left = playEnd - performance.now();
    timerEl.textContent = fmt(left);
    if (left <= 0) {
      var done = playing;
      stopNormal();
      liveEl.textContent = CUES[done].name + " — done";
      scheduleAuto(900);
      return;
    }
    raf = requestAnimationFrame(tick);
  }

  function fire(i) {
    var cue = CUES[i];
    if (i === next) next = (next + 1) % CUES.length;
    setCursor();

    if (cue.type === "bg") {
      cells[i].classList.add("bg-running", "played");
      liveEl.textContent = cue.name + " — looping underneath";
      scheduleAuto(1600);
    } else if (cue.type === "stop") {
      cells[i].classList.add("played");
      var t = cue.target;
      if (cells[t].classList.contains("bg-running")) {
        cells[t].classList.remove("bg-running");
        liveEl.textContent = CUES[t].name + " — fading out";
      } else {
        liveEl.textContent = cue.name + " — nothing to stop";
      }
      scheduleAuto(1600);
    } else {
      stopNormal();
      playing = i;
      playEnd = performance.now() + cue.dur;
      liveEl.textContent = "▶ " + cue.name;
      var f = cells[i].querySelector(".dfill");
      clearFill(i);
      // reflow so the reset lands before the animated run starts
      void f.offsetWidth;
      f.style.transition = reduced ? "none" : "transform " + cue.dur + "ms linear";
      f.style.transform = "translateX(0)";
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(tick);
    }

    if (i === 0 && next === 1 && allPlayed()) resetSoon();
  }

  function allPlayed() {
    return cells.every(function (el) { return el.classList.contains("played"); });
  }

  function resetShow() {
    stopNormal();
    cells.forEach(function (el, i) {
      el.classList.remove("played", "bg-running");
      clearFill(i);
    });
    next = 0;
    setCursor();
    timerEl.textContent = "0:00.0";
    liveEl.textContent = "Ready — GO fires the next cue";
  }

  function resetSoon() {
    clearTimeout(autoTimer);
    autoTimer = setTimeout(function () {
      resetShow();
      scheduleAuto(1400);
    }, 2200);
  }

  function scheduleAuto(delay) {
    if (!autopilot) return;
    clearTimeout(autoTimer);
    autoTimer = setTimeout(function () {
      if (!autopilot) return;
      if (allPlayed()) { resetShow(); scheduleAuto(1400); return; }
      fire(next);
    }, delay);
  }

  goBtn.addEventListener("click", function () {
    autopilot = false;
    clearTimeout(autoTimer);
    if (allPlayed()) { resetShow(); return; }
    fire(next);
  });

  setCursor();
  scheduleAuto(1500);
})();
