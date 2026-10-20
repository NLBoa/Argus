// Argus client. Small on purpose: theme, keyboard, palette, toasts, optimistic updates,
// offline state, the running timer, the "More" menus, and the workload readout. Everything
// else is server-rendered HTML over HTMX.
(() => {
  "use strict";
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const THEME_COOKIE = "argus_theme";

  const readBoot = () => {
    try { return JSON.parse($("#argus-boot")?.textContent || "{}"); } catch { return {}; }
  };
  // Pages the setup cannot support (e.g. Announcements with no staff-post source): skip them.
  const hiddenPages = () => new Set(readBoot().pages || []);
  const setCookie = (name, value) => {
    document.cookie = `${name}=${encodeURIComponent(value)}; Path=/; Max-Age=31536000; SameSite=Lax`;
  };
  const getCookie = (name) => {
    const m = document.cookie.match(new RegExp(`(?:^|; )${name}=([^;]*)`));
    return m ? decodeURIComponent(m[1]) : null;
  };
  const typing = (el) => !!el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));

  const COMMANDS = [
    { id: "go-today", label: "Go to Today", hint: "g t", href: "/today" },
    { id: "go-upcoming", label: "Go to Upcoming", hint: "g u", href: "/upcoming" },
    { id: "go-announcements", label: "Go to Announcements", hint: "g a", href: "/announcements" },
    { id: "go-workload", label: "Go to Workload", hint: "g w", href: "/workload" },
    { id: "go-health", label: "Go to Health", hint: "g h", href: "/health" },
    { id: "go-exams", label: "Go to Exam prep", hint: "g e", href: "/exams" },
    { id: "go-proposals", label: "Review vault proposals", hint: "g p", href: "/proposals" },
    { id: "go-settings", label: "Go to Settings (when you study)", hint: "", href: "/settings" },
    { id: "go-search", label: "Search", hint: "/", href: "/search" },
    { id: "go-styleguide", label: "Open the styleguide", hint: "", href: "/styleguide" },
    { id: "go-kiosk", label: "Open kiosk view", hint: "", href: "/kiosk" },
    { id: "sync", label: "Sync all sources now", hint: "", action: "sync" },
    { id: "theme-light", label: "Use light theme", hint: "", action: "theme:light" },
    { id: "theme-dark", label: "Use dark theme", hint: "", action: "theme:dark" },
    { id: "theme-system", label: "Match system theme", hint: "", action: "theme:system" },
  ];
  const GO = { t: "/today", u: "/upcoming", h: "/health", s: "/search", a: "/announcements", w: "/workload", e: "/exams", p: "/proposals" };

  const navigate = (href) => {
    if (window.htmx && href.startsWith("/") && !href.startsWith("/kiosk")) {
      window.htmx.ajax("GET", href, { target: "main", select: "main", swap: "outerHTML show:window:top" })
        .then(() => history.pushState({}, "", href));
    } else {
      window.location.href = href;
    }
  };

  document.addEventListener("alpine:init", () => {
    const Alpine = window.Alpine;

    Alpine.store("theme", {
      mode: getCookie(THEME_COOKIE) || "system",
      get label() {
        return { system: "Theme: match system", light: "Theme: light", dark: "Theme: dark" }[this.mode];
      },
      set(mode) {
        this.mode = mode;
        setCookie(THEME_COOKIE, mode);
        if (mode === "system") delete document.documentElement.dataset.theme;
        else document.documentElement.dataset.theme = mode;
      },
      cycle() { this.set({ system: "light", light: "dark", dark: "system" }[this.mode]); },
    });

    Alpine.store("net", { offline: !navigator.onLine });

    Alpine.store("toasts", {
      items: [],
      seq: 0,
      push(message, kind = "ok") {
        const id = ++this.seq;
        this.items.push({ id, message, kind });
        setTimeout(() => this.dismiss(id), kind === "error" ? 8000 : 4500);
      },
      dismiss(id) { this.items = this.items.filter((t) => t.id !== id); },
    });

    Alpine.store("palette", {
      open: false,
      opener: null,
      show() { this.opener = document.activeElement; this.open = true; },
      hide() {
        this.open = false;
        const back = this.opener;
        this.opener = null;
        if (back && document.contains(back)) requestAnimationFrame(() => back.focus());
      },
    });

    Alpine.store("keys", {
      pending: null,
      handle(e) {
        const palette = Alpine.store("palette");
        if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
          e.preventDefault();
          palette.open ? palette.hide() : palette.show();
          return;
        }
        if (palette.open || typing(e.target) || e.metaKey || e.ctrlKey || e.altKey) return;
        if (this.pending === "g") {
          this.pending = null;
          if (GO[e.key] && !hiddenPages().has(GO[e.key])) { e.preventDefault(); navigate(GO[e.key]); }
          return;
        }
        if (e.key === "g") { this.pending = "g"; setTimeout(() => { this.pending = null; }, 1200); return; }
        if (e.key === "/") {
          e.preventDefault();
          const box = $("#search-q") || $("#site-search");
          if (box && box.offsetParent !== null) { box.focus(); box.select(); } else navigate("/search");
          return;
        }
        if (e.key === "j" || e.key === "k") {
          const targets = $$("[data-nav-item] [data-nav-target]");
          if (!targets.length) return;
          e.preventDefault();
          const current = targets.findIndex((t) => t === document.activeElement || t.closest("[data-nav-item]")?.contains(document.activeElement));
          const next = current === -1 ? 0 : Math.max(0, Math.min(targets.length - 1, current + (e.key === "j" ? 1 : -1)));
          targets[next].focus();
          targets[next].closest("[data-nav-item]")?.scrollIntoView({ block: "nearest" });
        }
      },
    });

    let cachedItems = null;
    Alpine.data("palette", () => ({
      query: "",
      index: 0,
      items: cachedItems || [],
      init() {
        this.$nextTick(() => this.$refs.input?.focus());
        this.$watch("query", () => { this.index = 0; });
        if (!cachedItems) {
          fetch("/palette.json", { headers: { Accept: "application/json" } })
            .then((r) => (r.ok ? r.json() : { items: [] }))
            .then((d) => { cachedItems = d.items || []; this.items = cachedItems; })
            .catch(() => { this.items = []; });
        }
      },
      get results() {
        const q = this.query.trim().toLowerCase();
        const hidden = hiddenPages();
        const all = [...COMMANDS.filter((c) => !hidden.has(c.href)), ...this.items];
        if (!q) return all.slice(0, 12);
        const words = q.split(/\s+/);
        return all.filter((o) => words.every((w) => `${o.label} ${o.hint}`.toLowerCase().includes(w))).slice(0, 20);
      },
      get active() { return this.results[this.index]; },
      get activeId() { return this.results.length ? `palette-opt-${this.index}` : null; },
      move(d) {
        const n = this.results.length;
        if (!n) return;
        this.index = (this.index + d + n) % n;
        this.$nextTick(() => $(`#palette-opt-${this.index}`)?.scrollIntoView({ block: "nearest" }));
      },
      run(opt) {
        if (!opt) return;
        Alpine.store("palette").hide();
        if (opt.href) navigate(opt.href);
        else if (opt.action === "sync") window.htmx?.ajax("POST", "/sync", { swap: "none" });
        else if (opt.action?.startsWith("theme:")) Alpine.store("theme").set(opt.action.slice(6));
      },
      close() { Alpine.store("palette").hide(); },
    }));

    Alpine.data("timer", (elapsedSeconds) => ({
      started: performance.now() - elapsedSeconds * 1000,
      text: "",
      init() {
        const tick = () => {
          const s = Math.max(0, Math.floor((performance.now() - this.started) / 1000));
          const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
          this.text = h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`;
        };
        tick();
        const id = setInterval(tick, 1000);
        this.$el.addEventListener("htmx:beforeCleanupElement", () => clearInterval(id), { once: true });
      },
    }));
  });

  // ---------------------------------------------------------------- HTMX integration
  const toast = (message, kind) => window.Alpine?.store("toasts").push(message, kind);
  document.addEventListener("toast", (e) => toast(e.detail.message, e.detail.kind));

  document.addEventListener("htmx:beforeRequest", (e) => {
    const elt = e.detail.elt;
    if (e.detail.boosted || elt?.closest?.(".nav")) document.body.classList.add("is-loading");
    const form = elt?.closest?.("[data-optimistic]");
    if (form?.dataset.optimistic === "done") {
      const row = form.closest(".item-row");
      if (row) {
        row.dataset.previous = row.dataset.done;
        row.dataset.done = row.dataset.done === "true" ? "false" : "true";
      }
    }
  });
  const rollback = (e) => {
    const row = e.detail.elt?.closest?.(".item-row");
    if (row && row.dataset.previous !== undefined) {
      row.dataset.done = row.dataset.previous;
      delete row.dataset.previous;
    }
  };
  document.addEventListener("htmx:responseError", (e) => {
    rollback(e);
    document.body.classList.remove("is-loading");
    const status = e.detail.xhr?.status;
    toast(status === 404 ? "That item no longer exists. Refresh to update." : "That didn't save. Your change was undone.", "error");
  });
  document.addEventListener("htmx:sendError", (e) => {
    rollback(e);
    document.body.classList.remove("is-loading");
    if (window.Alpine) window.Alpine.store("net").offline = true;
    toast("Couldn't reach Argus. Your change was undone.", "error");
  });
  // A row's "..." menu copy of a control: after the row swaps, reopen the menu and refocus it
  // (htmx's own focus-by-id fails quietly inside a closed <details>).
  let menuFocus = null;
  document.addEventListener("htmx:beforeSwap", () => {
    const a = document.activeElement;
    menuFocus = a?.id && a.closest?.(".item-row__menu") ? a.id : null;
  });
  document.addEventListener("htmx:afterSettle", (e) => {
    document.body.classList.remove("is-loading");
    if (menuFocus) {
      const el = document.getElementById(menuFocus);
      menuFocus = null;
      const menu = el?.closest("details");
      if (menu) menu.open = true;
      el?.focus();
    }
    if (e.detail.boosted || e.detail.target?.id === "main") {
      const path = window.location.pathname;
      $$(".nav-link, .tabbar__link, .more__link").forEach((a) => {
        if (a.getAttribute("href") === path) a.setAttribute("aria-current", "page");
        else a.removeAttribute("aria-current");
      });
      // The header sits outside <main>: its proposal count follows the page just loaded.
      const count = Number($("#main")?.dataset.proposals || 0);
      $$("[data-proposal-count]").forEach((b) => { b.textContent = count; b.hidden = !count; });
      $$("[data-proposal-label]").forEach((l) => {
        l.textContent = count ? `, ${count} note edit${count === 1 ? "" : "s"} to review` : "";
      });
      $$("details[data-more]").forEach((d) => {
        d.open = false;
        const here = !!$(".more__link[aria-current]", d);
        if (here) d.firstElementChild.dataset.active = "true";
        else delete d.firstElementChild.dataset.active;
      });
      const main = $("#main");
      if (main && e.detail.boosted) main.focus({ preventScroll: true });
    }
    // The queue (D064): after a swap the server names the control that keeps focus, as
    // "<container id>:<key>"; the visible one gets it (rows have an inline and a menu copy).
    const queue = e.detail.target?.id === "queue" ? $("#queue") : null;
    const [where, key] = (queue?.dataset.focus || "").split(":");
    // htmx restores focus by id, which fails quietly for a control inside a closed menu.
    // offsetParent isn't enough: a control inside a closed <details> still has one.
    const shown = (x) => (x.checkVisibility ? x.checkVisibility() : x.offsetParent !== null);
    const active = document.activeElement;
    const kept = active?.closest?.("#queue") && shown(active);
    if (where && key && !kept) {
      const box = document.getElementById(where);
      const usable = () => box && $$(`[data-focus-key="${key}"]`, box).find((x) => shown(x) && !x.disabled);
      // On touch and narrow screens the controls live in the row's "..." menu: reopen it.
      const menu = box?.querySelector(".q-card__menu");
      if (!usable() && menu && getComputedStyle(menu).display !== "none") menu.open = true;
      const el = usable();
      (el || box?.querySelector("a, button:not([disabled])") || queue.querySelector("button:not([disabled])"))?.focus();
    }
  });

  // ---------------------------------------------------------------- "More" menus (D067)
  // A <details>: Enter or a click opens it; Esc closes it and returns focus to "More"; a click
  // outside closes it; opening one closes the other.
  const closeMore = (except) => $$("details[data-more][open]").forEach((d) => { if (d !== except) d.open = false; });
  document.addEventListener("toggle", (e) => {
    if (e.target.matches?.("details[data-more]") && e.target.open) closeMore(e.target);
  }, true);
  document.addEventListener("click", (e) => { if (!e.target.closest?.("details[data-more]")) closeMore(); });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    const menu = e.target.closest?.("details[data-more][open]");
    if (menu) { menu.open = false; menu.firstElementChild.focus(); } else closeMore();
  });

  // ---------------------------------------------------------------- workload chart (D065)
  // One readout for the day under the pointer, or the day picked with the arrow keys.
  const tips = new WeakMap();
  const tipsOf = (plot) => {
    if (!tips.has(plot)) { try { tips.set(plot, JSON.parse(plot.dataset.tips || "[]")); } catch { tips.set(plot, []); } }
    return tips.get(plot);
  };
  const wlShow = (plot, i) => {
    const days = tipsOf(plot);
    if (!days.length) return;
    i = Math.max(0, Math.min(days.length - 1, i));
    plot.dataset.i = i;
    const [day, due, win, short] = days[i];
    const tip = $(".wl__tip", plot), cross = $(".wl__cross", plot);
    const when = `By the end of ${/^(Today|Tomorrow)$/.test(day) ? day.toLowerCase() : day}`;
    $(".wl__tip-day", tip).textContent = when;
    $(".wl__tip-due", tip).textContent = due;
    $(".wl__tip-window", tip).textContent = win;
    $(".wl__tip-winrow", tip).hidden = !win;
    const sh = $(".wl__tip-short", tip);
    sh.hidden = !short;
    $("strong", sh).textContent = short;
    // The visual readout is aria-hidden; this always-present live region speaks it.
    const live = $("[data-wl-live]", plot.closest(".wl"));
    if (live) live.textContent = `${when}: ${due} work due${win ? `, ${win} window time` : ""}${short ? `, ${short} short` : ""}.`;
    cross.style.left = tip.style.left = `${((i + 0.5) / days.length) * 100}%`;
    tip.dataset.edge = i < 2 ? "start" : i > days.length - 3 ? "end" : "";
    tip.hidden = cross.hidden = false;
  };
  const wlHide = (plot) => { $(".wl__tip", plot).hidden = $(".wl__cross", plot).hidden = true; };
  document.addEventListener("pointermove", (e) => {
    const plot = e.target.closest?.(".wl__plot");
    if (!plot) return;
    const r = plot.getBoundingClientRect();
    wlShow(plot, Math.floor(((e.clientX - r.left) / r.width) * tipsOf(plot).length));
  });
  document.addEventListener("pointerout", (e) => {
    const plot = e.target.closest?.(".wl__plot");
    if (plot && !plot.contains(e.relatedTarget) && document.activeElement !== plot) wlHide(plot);
  });
  document.addEventListener("focusin", (e) => { if (e.target.matches?.(".wl__plot")) wlShow(e.target, +(e.target.dataset.i || 0)); });
  document.addEventListener("focusout", (e) => { if (e.target.matches?.(".wl__plot")) wlHide(e.target); });
  document.addEventListener("keydown", (e) => {
    const plot = e.target.matches?.(".wl__plot") ? e.target : null;
    const step = { ArrowLeft: -1, ArrowRight: 1, Home: -99, End: 99 }[e.key];
    if (!plot || step === undefined) return;
    e.preventDefault();
    wlShow(plot, +(plot.dataset.i || 0) + step);
  });

  // ---------------------------------------------------------------- offline
  const setOffline = (offline) => { if (window.Alpine) window.Alpine.store("net").offline = offline; };
  window.addEventListener("online", () => setOffline(false));
  window.addEventListener("offline", () => setOffline(true));

  if ("serviceWorker" in navigator && readBoot().page !== "/kiosk") {
    window.addEventListener("load", () => navigator.serviceWorker.register("/sw.js").catch(() => {}));
  }
})();
