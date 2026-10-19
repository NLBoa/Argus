// Argus client. Small on purpose: theme, keyboard, palette, toasts, optimistic updates,
// offline state, and the running timer. Everything else is server-rendered HTML over HTMX.
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
  document.addEventListener("htmx:afterSettle", (e) => {
    document.body.classList.remove("is-loading");
    if (e.detail.boosted || e.detail.target?.id === "main") {
      const path = window.location.pathname;
      $$(".nav-link, .tabbar__link").forEach((a) => {
        if (a.getAttribute("href") === path) a.setAttribute("aria-current", "page");
        else a.removeAttribute("aria-current");
      });
      const main = $("#main");
      if (main && e.detail.boosted) main.focus({ preventScroll: true });
    }
  });

  // ---------------------------------------------------------------- offline
  const setOffline = (offline) => { if (window.Alpine) window.Alpine.store("net").offline = offline; };
  window.addEventListener("online", () => setOffline(false));
  window.addEventListener("offline", () => setOffline(true));

  if ("serviceWorker" in navigator && readBoot().page !== "/kiosk") {
    window.addEventListener("load", () => navigator.serviceWorker.register("/sw.js").catch(() => {}));
  }
})();
