/* Legacy — command reference page: render, search, filter, deep-link */

(() => {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];

  const listEl = $("#cmd-groups");
  const sideEl = $("#cmd-side-links");
  const searchEl = $("#cmd-search");
  const emptyEl = $("#cmd-empty");
  const countEl = $("#cmd-count");
  if (!listEl) return;

  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const BADGES = {
    member: ["badge-member", "Members"],
    commissioner: ["badge-commissioner", "Commissioner"],
    admin: ["badge-admin", "Manage Server"],
    mixed: ["badge-mixed", "View: all · Manage: comm."],
  };
  const slug = (name) => name.replace(/^\//, "");
  const icon = (name, className = "site-icon") =>
    `<svg class="${className}" aria-hidden="true"><use href="assets/img/icons.svg#${esc(name)}"></use></svg>`;

  /* highlight parameters inside syntax: tokens like name:<...> or [optional] */
  const prettySyntax = (syntax) =>
    esc(syntax)
      .replace(/(\[[^\]]+\])/g, '<span class="p">$1</span>')
      .replace(/(\b[\w-]+:)(?=&lt;)/g, '<span class="p">$1</span>');

  /* ── render ── */
  const render = () => {
    listEl.innerHTML = LEGACY_CATEGORIES.map((cat) => {
      const cmds = LEGACY_COMMANDS.filter((c) => c.cat === cat.id);
      const cards = cmds.map((c) => {
        const [bCls, bLabel] = BADGES[c.access];
        const notes = c.notes && c.notes.length
          ? `<button class="cmd-notes-toggle" data-acc>Usage notes · ${c.notes.length} <span class="chev">▾</span></button>`
          : "";
        const notesPanel = c.notes && c.notes.length
          ? `<div class="cmd-notes"><div class="cmd-notes-inner"><ul>${c.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul></div></div>`
          : "";
        return `
        <article class="glass cmd-card" id="${slug(c.name)}" data-name="${esc(c.name)}" data-access="${c.access}"
                 data-search="${esc((c.name + " " + c.purpose + " " + c.syntax + " " + c.audience).toLowerCase())}">
          <div class="cmd-card-main">
            <div class="cmd-top">
              <span class="cmd-name"><span class="slash">/</span>${esc(c.name.slice(1))}</span>
              <div class="cmd-badges"><span class="badge ${bCls}">${bLabel}</span></div>
            </div>
            <p class="cmd-purpose">${esc(c.purpose)}</p>
            <div class="cmd-syntax">${prettySyntax(c.syntax)}<button class="copy-btn" data-copy="${esc(c.example)}" title="Copy example" aria-label="Copy example">⧉</button></div>
            <div class="cmd-meta"><b>Example</b><span style="font-family:var(--font-mono);font-size:12px;word-break:break-word">${esc(c.example)}</span></div>
            ${notes}
          </div>
          ${notesPanel}
        </article>`;
      }).join("");
      return `
      <section class="cmd-group" id="cat-${cat.id}" data-cat="${cat.id}">
        <div class="cmd-group-head">
          <h2>${icon(cat.icon)} ${esc(cat.name)}</h2>
          <span class="g-blurb">${esc(cat.blurb)}</span>
        </div>
        <div class="cmd-list">${cards}</div>
      </section>`;
    }).join("");

    sideEl.innerHTML = LEGACY_CATEGORIES.map((cat) => {
      const n = LEGACY_COMMANDS.filter((c) => c.cat === cat.id).length;
      return `<a href="#cat-${cat.id}" data-side="${cat.id}"><span class="c-ico">${icon(cat.icon)}</span>${esc(cat.name)}<span class="c-count">${n}</span></a>`;
    }).join("") + `<div class="side-note">Press <kbd style="font-family:var(--font-mono);font-size:11px;border:1px solid rgba(168,197,255,.25);border-radius:5px;padding:1px 6px">/</kbd> to search · click a card's ⧉ to copy its example</div>`;
  };
  render();
  if (countEl) countEl.textContent = LEGACY_COMMANDS.length;

  /* ── filter state ── */
  let query = "", access = "all";
  const applyFilter = () => {
    let visible = 0;
    $$(".cmd-card", listEl).forEach((card) => {
      const okQ = !query || card.dataset.search.includes(query);
      const okA = access === "all" || card.dataset.access === access ||
        (access === "commissioner" && (card.dataset.access === "admin" || card.dataset.access === "mixed")) ||
        (access === "member" && card.dataset.access === "mixed");
      const show = okQ && okA;
      card.style.display = show ? "" : "none";
      if (show) visible++;
    });
    $$(".cmd-group", listEl).forEach((group) => {
      const any = $$(".cmd-card", group).some((c) => c.style.display !== "none");
      group.style.display = any ? "" : "none";
    });
    emptyEl.style.display = visible ? "none" : "block";
  };

  searchEl.addEventListener("input", () => { query = searchEl.value.trim().toLowerCase(); applyFilter(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && document.activeElement !== searchEl && !/INPUT|TEXTAREA/.test(document.activeElement.tagName)) {
      e.preventDefault(); searchEl.focus();
    }
    if (e.key === "Escape" && document.activeElement === searchEl) { searchEl.value = ""; query = ""; applyFilter(); searchEl.blur(); }
  });

  $$("[data-access-pill]").forEach((pill) => {
    pill.addEventListener("click", () => {
      $$("[data-access-pill]").forEach((p) => p.classList.remove("active"));
      pill.classList.add("active");
      access = pill.dataset.accessPill;
      applyFilter();
    });
  });

  /* ── scrollspy for sidebar ── */
  const sideLinks = $$("[data-side]", sideEl);
  const spy = new IntersectionObserver((entries) => {
    entries.forEach((e) => {
      if (!e.isIntersecting) return;
      sideLinks.forEach((l) => l.classList.toggle("active", l.dataset.side === e.target.dataset.cat));
    });
  }, { rootMargin: "-20% 0px -70% 0px" });
  $$(".cmd-group", listEl).forEach((g) => spy.observe(g));

  /* ── deep link: /commands.html#setup scrolls + flashes the card ── */
  const flashHash = () => {
    const id = decodeURIComponent(location.hash.slice(1));
    if (!id) return;
    const card = document.getElementById(id);
    if (!card || !card.classList.contains("cmd-card")) return;
    setTimeout(() => {
      card.scrollIntoView({ behavior: "smooth", block: "center" });
      card.classList.add("flash");
      setTimeout(() => card.classList.remove("flash"), 2600);
    }, 120);
  };
  window.addEventListener("hashchange", flashHash);
  flashHash();
})();
