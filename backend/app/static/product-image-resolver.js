"use strict";

(() => {
  const storageKey = "leo_crm_service_token";
  const queue = [];
  const scheduledProductIds = new Set();
  let activeRequests = 0;

  const errorFrom = async (response) => {
    let detail = `HTTP ${response.status}`;
    try {
      const payload = await response.json();
      detail = typeof payload.detail === "string" ? payload.detail : detail;
    } catch (_) {}
    return new Error(detail);
  };

  const targetsFor = (productId) => document.querySelectorAll(
    `[data-resolve-product-image][data-product-id="${productId}"]`,
  );

  const finish = (productId, imageUrl = null, errorMessage = "", pending = false) => {
    targetsFor(productId).forEach((target) => {
      target.removeAttribute("data-resolve-product-image");
      if (!imageUrl) {
        if (target.tagName !== "IMG") target.textContent = pending ? "Ожидает Agent" : "Нет фото";
        if (errorMessage) target.title = errorMessage;
        return;
      }
      if (target.tagName === "IMG") {
        target.src = imageUrl;
        target.classList.remove("hidden");
        return;
      }
      const photo = document.createElement("img");
      photo.className = target.dataset.imageClass || "product-thumb";
      photo.src = imageUrl;
      photo.alt = "";
      photo.loading = "lazy";
      photo.decoding = "async";
      photo.referrerPolicy = "no-referrer";
      target.replaceWith(photo);
    });
  };

  const pump = () => {
    while (activeRequests < 2 && queue.length > 0) {
      const productId = queue.shift();
      activeRequests += 1;
      fetch(`/api/product-registry/products/${productId}/resolve-image`, {
        method: "POST",
        cache: "no-store",
        headers: {Authorization: `Bearer ${localStorage.getItem(storageKey) || ""}`},
      })
        .then(async (response) => {
          if (!response.ok) throw await errorFrom(response);
          return response.json();
        })
        .then((payload) => finish(productId, payload.image_url, "", Boolean(payload.pending)))
        .catch((error) => finish(productId, null, error.message || "Фото Kaspi не получено"))
        .finally(() => { activeRequests -= 1; pump(); });
    }
  };

  const observer = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting) return;
      observer.unobserve(entry.target);
      const productId = Number(entry.target.dataset.productId || 0);
      if (!productId || scheduledProductIds.has(productId)) return;
      scheduledProductIds.add(productId);
      queue.push(productId);
    });
    pump();
  }, {rootMargin: "160px 0px"});

  const observe = (root = document) => {
    if (!root?.querySelectorAll) return;
    root.querySelectorAll("[data-resolve-product-image][data-product-id]").forEach((target) => observer.observe(target));
    if (root.matches?.("[data-resolve-product-image][data-product-id]")) observer.observe(root);
  };

  const start = () => {
    observe(document);
    new MutationObserver((mutations) => mutations.forEach((mutation) => mutation.addedNodes.forEach(observe)))
      .observe(document.body, {childList: true, subtree: true});
  };

  window.LEOProductImageResolver = {observe};
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start, {once: true});
  else start();
})();
