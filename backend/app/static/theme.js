(() => {
  "use strict";

  const storageKey = "leo.crm.interface-theme";
  const blueTheme = "blue";
  const lightTheme = "light";
  const stylesheet = document.querySelector("#light-theme-stylesheet");

  const storedTheme = (() => {
    try {
      return localStorage.getItem(storageKey);
    } catch {
      return null;
    }
  })();

  const normalizeTheme = (theme) => theme === lightTheme ? lightTheme : blueTheme;

  const applyTheme = (theme, persist = false) => {
    const nextTheme = normalizeTheme(theme);
    document.documentElement.dataset.theme = nextTheme;
    document.documentElement.style.colorScheme = nextTheme === lightTheme ? "light" : "dark";
    if (stylesheet) stylesheet.media = nextTheme === lightTheme ? "all" : "not all";
    if (persist) {
      try {
        localStorage.setItem(storageKey, nextTheme);
      } catch {
        // The theme still works for the current page when storage is unavailable.
      }
    }
    document.querySelectorAll("[data-interface-theme-toggle]").forEach((input) => {
      input.checked = nextTheme === lightTheme;
      input.setAttribute("aria-checked", String(input.checked));
    });
    return nextTheme;
  };

  applyTheme(storedTheme);

  const mountToggle = () => {
    const sidebar = document.querySelector(".sidebar");
    if (!sidebar || sidebar.querySelector("[data-theme-toggle]")) return;

    const container = document.createElement("div");
    container.className = "theme-toggle";
    container.dataset.themeToggle = "";
    container.innerHTML = `
      <span>Белый фон</span>
      <label class="theme-toggle-switch">
        <input type="checkbox" data-interface-theme-toggle aria-label="Включить белый интерфейс">
        <span class="theme-toggle-track" aria-hidden="true"></span>
      </label>
    `;

    const runtimeStatus = sidebar.querySelector(".runtime-status");
    sidebar.insertBefore(container, runtimeStatus || null);
    const input = container.querySelector("[data-interface-theme-toggle]");
    input.checked = document.documentElement.dataset.theme === lightTheme;
    input.setAttribute("aria-checked", String(input.checked));
    input.addEventListener("change", () => {
      applyTheme(input.checked ? lightTheme : blueTheme, true);
    });
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mountToggle, { once: true });
  } else {
    mountToggle();
  }
})();
