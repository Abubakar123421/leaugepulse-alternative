/* ══════════════════════════════════════════════════════════════════
   LEGACY — shared interactions + cinematic hero
   ══════════════════════════════════════════════════════════════════ */

(() => {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ── Nav: scrolled state ── */
  const navShell = $(".nav-shell");
  const onScrollNav = () => navShell && navShell.classList.toggle("scrolled", window.scrollY > 24);
  onScrollNav();
  window.addEventListener("scroll", onScrollNav, { passive: true });

  /* ── Nav: active link ── */
  const page = document.body.dataset.page;
  $$(".nav-links a, .mobile-menu a").forEach((a) => {
    if (a.dataset.nav === page) a.classList.add("active");
  });

  /* ── Burger ── */
  const burger = $(".nav-burger");
  if (burger) {
    burger.addEventListener("click", () => document.body.classList.toggle("menu-open"));
    $$(".mobile-menu a").forEach((a) => a.addEventListener("click", () => document.body.classList.remove("menu-open")));
  }

  /* ── Reveal on scroll ── */
  const io = new IntersectionObserver(
    (entries) => entries.forEach((e) => { if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); } }),
    { threshold: 0.12, rootMargin: "0px 0px -6% 0px" }
  );
  $$(".reveal").forEach((el) => io.observe(el));

  /* ── Animated counters ── */
  const fmt = new Intl.NumberFormat("en-US");
  const cio = new IntersectionObserver((entries) => {
    entries.forEach((e) => {
      if (!e.isIntersecting) return;
      cio.unobserve(e.target);
      const el = e.target, target = parseFloat(el.dataset.count), suffix = el.dataset.suffix || "";
      if (reduceMotion) { el.textContent = fmt.format(target) + suffix; return; }
      const t0 = performance.now(), dur = 1600;
      const tick = (t) => {
        const p = Math.min(1, (t - t0) / dur), ease = 1 - Math.pow(1 - p, 4);
        el.textContent = fmt.format(Math.round(target * ease)) + suffix;
        if (p < 1) requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    });
  }, { threshold: 0.5 });
  $$("[data-count]").forEach((el) => cio.observe(el));

  /* ── Copy buttons (delegated) ── */
  document.addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-copy]");
    if (!btn) return;
    navigator.clipboard.writeText(btn.dataset.copy).then(() => {
      btn.classList.add("copied");
      const prev = btn.innerHTML;
      btn.innerHTML = "✓";
      setTimeout(() => { btn.classList.remove("copied"); btn.innerHTML = prev; }, 1400);
    });
  });

  /* ── Accordions (FAQ + command notes) ── */
  document.addEventListener("click", (ev) => {
    const trigger = ev.target.closest("[data-acc]");
    if (!trigger) return;
    const item = trigger.closest(".faq-item, .cmd-card");
    const panel = item && item.querySelector(".faq-a, .cmd-notes");
    if (!panel) return;
    const open = item.classList.toggle("open");
    panel.style.maxHeight = open ? panel.scrollHeight + "px" : "0px";
  });

  /* ── Year ── */
  $$("[data-year]").forEach((el) => (el.textContent = new Date().getFullYear()));

  /* ══════════════════════════════════════════════════════════════
     Cinematic hero — stadium-night canvas, scroll scrub, and an
     auto-upgrading slot for an AI-generated video (assets/video/hero.mp4).
     ══════════════════════════════════════════════════════════════ */
  const heroTrack = $(".hero-track");
  if (!heroTrack) return;
  const hero = $(".hero", heroTrack);
  const media = $(".hero-media", heroTrack);
  const canvas = $("canvas", media);
  const heroCopy = $(".hero-inner", heroTrack);
  const heroBeat = $(".hero-beat", heroTrack);
  const scrollCue = $(".hero-scroll-cue", heroTrack);

  /* video slot: if a hero video exists, it replaces the canvas as a slow ambient
     loop — scroll drives parallax/zoom and the text beats, never the decoder
     (seeking per-frame stutters on sparsely-keyframed footage) */
  let video = null, videoReady = false;
  const tryVideo = () => {
    const v = document.createElement("video");
    v.muted = true; v.playsInline = true; v.preload = "auto"; v.loop = true; v.crossOrigin = "anonymous";
    ["assets/video/hero.mp4", "assets/video/hero.webm"].forEach((src) => {
      const s = document.createElement("source");
      s.src = src;
      v.appendChild(s);
    });
    v.addEventListener("loadeddata", () => {
      videoReady = true; video = v;
      canvas.style.display = "none";
      media.insertBefore(v, media.firstChild);
      v.playbackRate = 0.85;
      if (!reduceMotion) v.play().catch(() => {});
    }, { once: true });
    v.load();
  };
  tryVideo();


  /* scroll progress through the track */
  let prog = 0, smoothProg = 0;
  const measure = () => {
    const rect = heroTrack.getBoundingClientRect();
    const total = heroTrack.offsetHeight - hero.offsetHeight;
    prog = total > 0 ? Math.min(1, Math.max(0, -rect.top / total)) : 0;
  };
  window.addEventListener("scroll", measure, { passive: true });
  window.addEventListener("resize", measure);
  measure();

  const applyScrub = () => {
    /* copy fades out over first third, beat fades in over middle */
    const outP = Math.min(1, smoothProg / 0.32);
    const inP = Math.min(1, Math.max(0, (smoothProg - 0.42) / 0.3));
    if (heroCopy) {
      heroCopy.style.opacity = String(1 - outP);
      heroCopy.style.transform = `translateY(${outP * -46}px)`;
      heroCopy.style.pointerEvents = outP > 0.6 ? "none" : "";
    }
    if (heroBeat) {
      heroBeat.style.opacity = String(inP);
      heroBeat.style.transform = `translateY(${(1 - inP) * 34}px) scale(${0.96 + inP * 0.04})`;
    }
    if (scrollCue) scrollCue.style.opacity = String(1 - Math.min(1, smoothProg / 0.12));
    if (videoReady && video) {
      /* GPU-cheap parallax zoom instead of decoder seeks */
      video.style.transform = `translateY(${smoothProg * -4}%) scale(${1 + smoothProg * 0.14})`;
      const past = smoothProg > 0.995;
      if (past && !video.paused) video.pause();
      else if (!past && video.paused && !reduceMotion && !document.hidden) video.play().catch(() => {});
    }
  };
  /* ── canvas scene: field, beams, particles, streaks ── */
  const ctx = canvas.getContext("2d");
  let W = 0, H = 0, DPR = 1;
  const resize = () => {
    DPR = Math.min(2, window.devicePixelRatio || 1);
    W = media.clientWidth; H = media.clientHeight;
    canvas.width = W * DPR; canvas.height = H * DPR;
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  };
  resize();
  window.addEventListener("resize", resize);

  const rand = (a, b) => a + Math.random() * (b - a);
  const GOLD = "232,187,90", BLUE = "63,157,255", CYAN = "92,201,255";
  const particles = Array.from({ length: 110 }, () => ({
    x: Math.random(), y: Math.random(), z: rand(0.25, 1),
    r: rand(0.6, 2.3), s: rand(0.006, 0.024),
    hue: Math.random() < 0.42 ? GOLD : Math.random() < 0.5 ? BLUE : CYAN,
    tw: rand(0, Math.PI * 2),
  }));
  let streaks = [];
  const spawnStreak = () => {
    if (streaks.length > 2) return;
    const fromLeft = Math.random() < 0.5;
    streaks.push({
      x: fromLeft ? -0.1 : 1.1, y: rand(0.08, 0.42),
      vx: (fromLeft ? 1 : -1) * rand(0.35, 0.6), vy: rand(0.02, 0.07),
      life: 0, hue: Math.random() < 0.5 ? GOLD : CYAN,
    });
  };

  const drawField = (p, t) => {
    /* perspective yard lines rising from the bottom — the "stadium turf" */
    const horizon = H * (0.62 - p * 0.1);
    const zoom = 1 + p * 0.9;
    ctx.save();
    /* turf glow */
    const turf = ctx.createLinearGradient(0, horizon, 0, H);
    turf.addColorStop(0, "rgba(10,26,52,0)");
    turf.addColorStop(0.5, `rgba(13,32,64,${0.34 + p * 0.2})`);
    turf.addColorStop(1, `rgba(6,16,34,${0.55 + p * 0.25})`);
    ctx.fillStyle = turf;
    ctx.fillRect(0, horizon, W, H - horizon);

    /* horizontal yard lines with perspective spacing */
    ctx.lineWidth = 1;
    for (let i = 0; i < 11; i++) {
      const f = i / 10;
      const eased = Math.pow(f, 2.1);
      const y = horizon + eased * (H - horizon) * zoom;
      if (y > H + 40) continue;
      const alpha = 0.05 + f * 0.15;
      ctx.strokeStyle = `rgba(140,180,240,${alpha})`;
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
    }
    /* converging sidelines */
    const cx = W / 2;
    for (let i = -4; i <= 4; i++) {
      if (i === 0) continue;
      const spread = i * (W / 7);
      ctx.strokeStyle = `rgba(140,180,240,${0.10 - Math.abs(i) * 0.012})`;
      ctx.beginPath();
      ctx.moveTo(cx + spread * 0.22, horizon);
      ctx.lineTo(cx + spread * (1.15 + p * 0.55), H);
      ctx.stroke();
    }
    /* midfield glow pulse */
    const pulse = 0.5 + Math.sin(t * 0.0006) * 0.5;
    const mg = ctx.createRadialGradient(cx, horizon + (H - horizon) * 0.42, 0, cx, horizon + (H - horizon) * 0.42, W * 0.3);
    mg.addColorStop(0, `rgba(${BLUE},${0.05 + pulse * 0.05 + p * 0.05})`);
    mg.addColorStop(1, "rgba(63,157,255,0)");
    ctx.fillStyle = mg;
    ctx.fillRect(0, horizon, W, H - horizon);
    ctx.restore();
  };

  const drawBeams = (p, t) => {
    /* sweeping stadium light beams */
    const beams = [
      { x: W * 0.06, sway: 0.00014, base: 0.42, hue: BLUE, a: 0.05 },
      { x: W * 0.94, sway: 0.00011, base: -0.42, hue: GOLD, a: 0.045 },
      { x: W * 0.5, sway: 0.00008, base: 0.0, hue: CYAN, a: 0.035 },
    ];
    beams.forEach((b, i) => {
      const ang = b.base + Math.sin(t * b.sway + i * 2.1) * 0.22;
      const len = H * 1.25, wTop = 14, wBot = W * (0.16 + p * 0.05);
      ctx.save();
      ctx.translate(b.x, -30);
      ctx.rotate(ang);
      const g = ctx.createLinearGradient(0, 0, 0, len);
      g.addColorStop(0, `rgba(${b.hue},${(b.a + p * 0.03) * 1.6})`);
      g.addColorStop(1, `rgba(${b.hue},0)`);
      ctx.fillStyle = g;
      ctx.beginPath();
      ctx.moveTo(-wTop / 2, 0); ctx.lineTo(wTop / 2, 0);
      ctx.lineTo(wBot / 2, len); ctx.lineTo(-wBot / 2, len);
      ctx.closePath(); ctx.fill();
      ctx.restore();
    });
  };

  const drawParticles = (p, t, dt) => {
    particles.forEach((pt) => {
      pt.y -= pt.s * (0.4 + pt.z) * (1 + p * 1.6) * dt * 0.06;
      pt.x += Math.sin(t * 0.0004 + pt.tw) * 0.00022 * dt * 0.06;
      if (pt.y < -0.05) { pt.y = 1.05; pt.x = Math.random(); }
      const twinkle = 0.55 + Math.sin(t * 0.002 + pt.tw) * 0.45;
      const a = 0.16 * pt.z * twinkle + p * 0.08;
      ctx.beginPath();
      ctx.fillStyle = `rgba(${pt.hue},${a})`;
      ctx.arc(pt.x * W, pt.y * H, pt.r * (1 + p * 0.5), 0, Math.PI * 2);
      ctx.fill();
    });
    /* light streaks */
    if (Math.random() < 0.004) spawnStreak();
    streaks = streaks.filter((s) => s.life < 1);
    streaks.forEach((s) => {
      s.life += dt * 0.00042;
      s.x += s.vx * dt * 0.00042; s.y += s.vy * dt * 0.00042;
      const a = Math.sin(s.life * Math.PI) * 0.5;
      const x = s.x * W, y = s.y * H, tail = 130 * (s.vx > 0 ? -1 : 1);
      const g = ctx.createLinearGradient(x, y, x + tail, y - 26);
      g.addColorStop(0, `rgba(${s.hue},${a})`);
      g.addColorStop(1, `rgba(${s.hue},0)`);
      ctx.strokeStyle = g; ctx.lineWidth = 1.6;
      ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x + tail, y - 26); ctx.stroke();
    });
  };

  let last = performance.now();
  let running = true;
  const frame = (t) => {
    if (!running) return;
    const dt = Math.min(50, t - last); last = t;
    smoothProg += (prog - smoothProg) * 0.09;
    if (!videoReady) {
      ctx.clearRect(0, 0, W, H);
      drawBeams(smoothProg, t);
      drawField(smoothProg, t);
      drawParticles(smoothProg, t, dt);
    }
    applyScrub();
    requestAnimationFrame(frame);
  };
  if (reduceMotion) {
    /* static single frame + no scrub motion */
    drawBeams(0, 0); drawField(0, 0); drawParticles(0, 0, 16);
  } else {
    requestAnimationFrame(frame);
    document.addEventListener("visibilitychange", () => {
      running = !document.hidden;
      if (video) { if (document.hidden) video.pause(); }
      if (running) { last = performance.now(); requestAnimationFrame(frame); }
    });
  }

  /* gentle pointer parallax on the shield */
  const shieldWrap = $(".hero-art", heroTrack);
  const shield = $(".hero-shield", heroTrack);
  if (shieldWrap && shield && !reduceMotion && matchMedia("(pointer:fine)").matches) {
    hero.addEventListener("pointermove", (e) => {
      const r = hero.getBoundingClientRect();
      const dx = (e.clientX - r.left) / r.width - 0.5;
      const dy = (e.clientY - r.top) / r.height - 0.5;
      shield.style.transform = `translate(${dx * 18}px, ${dy * 14}px) rotate(${dx * 2.4}deg)`;
    });
    hero.addEventListener("pointerleave", () => { shield.style.transform = ""; });
  }
})();
