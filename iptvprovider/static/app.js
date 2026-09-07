/* IPTV Provider System - the small amount of JavaScript the panel needs. */
(function () {
  "use strict";

  const csrf = document.querySelector('meta[name="csrf"]')?.content || "";

  // ---------------------------------------------------------------- helpers
  function toast(message, isError) {
    let box = document.querySelector(".flashes");
    if (!box) {
      box = document.createElement("div");
      box.className = "flashes";
      document.body.appendChild(box);
    }
    const el = document.createElement("div");
    el.className = "flash" + (isError ? " flash--error" : "");
    el.textContent = message;
    box.appendChild(el);
    setTimeout(() => el.remove(), isError ? 9000 : 5000);
  }
  window.toast = toast;

  async function post(url, body) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF": csrf, "X-Requested-With": "fetch" },
      body: JSON.stringify(Object.assign({ _csrf: csrf }, body || {})),
    });
    let data = {};
    try { data = await response.json(); } catch (_) { /* not json */ }
    if (!response.ok || data.ok === false) {
      throw new Error(data.message || data.error || ("Request failed (" + response.status + ")"));
    }
    return data;
  }
  window.apiPost = post;

  // Flash messages from the server fade on their own.
  document.querySelectorAll(".flashes .flash").forEach((el) => {
    setTimeout(() => el.remove(), el.classList.contains("flash--error") ? 10000 : 6000);
  });

  // Confirmation on destructive forms and buttons.
  document.addEventListener("submit", (event) => {
    const form = event.target;
    const text = form.getAttribute("data-confirm");
    if (text && !window.confirm(text)) event.preventDefault();
  });

  // Copy buttons: <button data-copy="#id"> or data-copy-text="...".
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-copy], [data-copy-text]");
    if (!button) return;
    event.preventDefault();
    let text = button.getAttribute("data-copy-text");
    if (!text) {
      const target = document.querySelector(button.getAttribute("data-copy"));
      text = target ? (target.value !== undefined ? target.value : target.textContent) : "";
    }
    try {
      await navigator.clipboard.writeText(text);
      const old = button.textContent;
      button.textContent = "Copied";
      setTimeout(() => (button.textContent = old), 1400);
    } catch (_) {
      window.prompt("Copy this:", text);
    }
  });

  // Local time for <time data-utc="...">.
  document.querySelectorAll("time[data-utc]").forEach((el) => {
    const raw = el.getAttribute("data-utc");
    if (!raw) return;
    const date = new Date(raw);
    if (isNaN(date)) return;
    const mode = el.getAttribute("data-mode") || "datetime";
    if (mode === "date") {
      el.textContent = date.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
    } else if (mode === "ago") {
      el.textContent = ago(date);
      el.title = date.toLocaleString();
    } else {
      el.textContent = date.toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
    }
  });
  function ago(date) {
    const seconds = Math.round((Date.now() - date.getTime()) / 1000);
    if (seconds < 60) return "just now";
    const minutes = Math.round(seconds / 60);
    if (minutes < 60) return minutes + " min ago";
    const hours = Math.round(minutes / 60);
    if (hours < 48) return hours + " h ago";
    const days = Math.round(hours / 24);
    if (days < 60) return days + " d ago";
    return date.toLocaleDateString();
  }

  // Tabs: <button class="tab" data-tab="x"> shows [data-panel="x"].
  document.querySelectorAll("[data-tab]").forEach((tab) => {
    tab.addEventListener("click", () => {
      const group = tab.closest("[data-tabs]") || document;
      group.querySelectorAll("[data-tab]").forEach((t) => t.classList.toggle("is-on", t === tab));
      group.querySelectorAll("[data-panel]").forEach((p) => p.classList.toggle("hidden", p.getAttribute("data-panel") !== tab.getAttribute("data-tab")));
    });
  });

  // Modals.
  document.addEventListener("click", (event) => {
    const opener = event.target.closest("[data-open]");
    if (opener) {
      event.preventDefault();
      const modal = document.getElementById(opener.getAttribute("data-open"));
      if (modal) {
        modal.classList.add("is-open");
        const first = modal.querySelector("input, select, textarea");
        if (first) setTimeout(() => first.focus(), 30);
      }
    }
    const closer = event.target.closest("[data-close]");
    if (closer) {
      event.preventDefault();
      closer.closest(".modal")?.classList.remove("is-open");
    }
    if (event.target.classList && event.target.classList.contains("modal")) {
      event.target.classList.remove("is-open");
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") document.querySelectorAll(".modal.is-open").forEach((m) => m.classList.remove("is-open"));
  });

  // ------------------------------------------------------------ bulk select
  // A table with [data-bulk] gets a master checkbox, a count, and buttons that
  // post {action, ids} or {action, all: true, filter} to data-bulk-url.
  document.querySelectorAll("[data-bulk]").forEach((root) => {
    const url = root.getAttribute("data-bulk");
    const bar = root.querySelector(".bulkbar");
    const master = root.querySelector("[data-check-all]");
    const total = Number(root.getAttribute("data-total") || 0);
    const filter = JSON.parse(root.getAttribute("data-filter") || "{}");
    let allMode = false;

    const boxes = () => Array.from(root.querySelectorAll(".row-check"));
    const selected = () => boxes().filter((b) => b.checked).map((b) => b.value);

    function refresh() {
      const count = allMode ? total : selected().length;
      boxes().forEach((b) => b.closest("tr")?.classList.toggle("is-selected", b.checked || allMode));
      if (bar) {
        bar.classList.toggle("is-on", count > 0);
        const label = bar.querySelector("[data-count]");
        if (label) label.textContent = allMode ? `All ${total} matching selected` : `${count} selected`;
        const allLink = bar.querySelector("[data-select-all-matching]");
        if (allLink) allLink.classList.toggle("hidden", allMode || total <= boxes().length || selected().length !== boxes().length);
        const clear = bar.querySelector("[data-clear]");
        if (clear) clear.classList.toggle("hidden", count === 0);
      }
      if (master) {
        const some = selected().length;
        master.checked = some > 0 && some === boxes().length;
        master.indeterminate = some > 0 && some < boxes().length;
      }
    }

    master?.addEventListener("change", () => {
      boxes().forEach((b) => (b.checked = master.checked));
      allMode = false;
      refresh();
    });
    root.addEventListener("change", (event) => {
      if (event.target.classList.contains("row-check")) {
        allMode = false;
        refresh();
      }
    });
    bar?.querySelector("[data-select-all-matching]")?.addEventListener("click", (event) => {
      event.preventDefault();
      allMode = true;
      refresh();
    });
    bar?.querySelector("[data-clear]")?.addEventListener("click", (event) => {
      event.preventDefault();
      allMode = false;
      boxes().forEach((b) => (b.checked = false));
      refresh();
    });
    bar?.querySelectorAll("[data-action]").forEach((button) => {
      button.addEventListener("click", async () => {
        const action = button.getAttribute("data-action");
        const confirmText = button.getAttribute("data-confirm");
        const count = allMode ? total : selected().length;
        if (!count) return;
        if (confirmText && !window.confirm(confirmText.replace("{n}", count))) return;
        const body = { action };
        if (allMode) {
          body.all = true;
          body.filter = filter;
        } else {
          body.ids = selected();
        }
        const ask = button.getAttribute("data-ask");
        if (ask) {
          const answer = window.prompt(ask, button.getAttribute("data-ask-default") || "");
          if (answer === null) return;
          body[button.getAttribute("data-ask-key") || "value"] = answer;
        }
        button.disabled = true;
        try {
          const result = await post(url, body);
          toast(result.message || "Done");
          setTimeout(() => window.location.reload(), 400);
        } catch (error) {
          toast(error.message, true);
          button.disabled = false;
        }
      });
    });
    refresh();
  });

  // ------------------------------------------------------------ channel edit
  const editModal = document.getElementById("channel-edit");
  if (editModal) {
    const form = editModal.querySelector("form");
    document.addEventListener("click", async (event) => {
      const button = event.target.closest("[data-edit-channel]");
      if (!button) return;
      event.preventDefault();
      const id = button.getAttribute("data-edit-channel");
      try {
        const response = await fetch(`/channels/${id}.json`, { headers: { "X-Requested-With": "fetch" } });
        const data = await response.json();
        if (!data.ok) throw new Error(data.error || "Could not load");
        const c = data.channel;
        form.action = `/channels/${id}/update`;
        editModal.querySelector("[data-title]").textContent = c.name;
        editModal.querySelector("[data-source]").textContent = c.source_name + (c.edited ? " - edited" : "");
        ["name", "url", "group_name", "tvg_id", "tvg_logo", "duration", "extras"].forEach((key) => {
          const field = form.querySelector(`[name="${key}"]`);
          if (field) field.value = c[key] ?? "";
        });
        const extra = Object.entries(c.attrs || {}).filter(([k]) => !["group-title", "tvg-id", "tvg-logo"].includes(k.toLowerCase()));
        const attrsBox = editModal.querySelector("[data-attrs]");
        attrsBox.textContent = extra.length ? extra.map(([k, v]) => `${k}="${v}"`).join("  ") : "none";
        editModal.classList.add("is-open");
      } catch (error) {
        toast(error.message, true);
      }
    });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const body = Object.fromEntries(new FormData(form).entries());
      try {
        const result = await post(form.action, body);
        toast(result.message || "Saved");
        setTimeout(() => window.location.reload(), 300);
      } catch (error) {
        toast(error.message, true);
      }
    });
  }

  // Single-row actions posting JSON: <button data-post="/url" data-body='{"..."}'>
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-post]");
    if (!button) return;
    event.preventDefault();
    const confirmText = button.getAttribute("data-confirm");
    if (confirmText && !window.confirm(confirmText)) return;
    button.disabled = true;
    try {
      const body = JSON.parse(button.getAttribute("data-body") || "{}");
      const result = await post(button.getAttribute("data-post"), body);
      toast(result.message || "Done");
      if (button.hasAttribute("data-show-json")) {
        const target = document.querySelector(button.getAttribute("data-show-json"));
        if (target) {
          target.textContent = JSON.stringify(result.reply !== undefined ? result.reply : result, null, 2);
          target.closest(".hidden")?.classList.remove("hidden");
          target.parentElement?.classList.remove("hidden");
        }
        button.disabled = false;
        return;
      }
      setTimeout(() => window.location.reload(), 300);
    } catch (error) {
      toast(error.message, true);
      button.disabled = false;
    }
  });

  // Expiry quick-pick on the add-subscriber form.
  document.querySelectorAll("[data-days-select]").forEach((select) => {
    const custom = document.querySelector(select.getAttribute("data-days-select"));
    const update = () => custom?.classList.toggle("hidden", select.value !== "custom");
    select.addEventListener("change", update);
    update();
  });
})();
