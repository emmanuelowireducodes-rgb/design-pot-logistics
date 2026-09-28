(function (root) {
  "use strict";

  function moveBefore(order, draggedId, targetId, after) {
    const next = order.filter((id) => id !== draggedId);
    const targetIndex = next.indexOf(targetId);
    if (targetIndex < 0 || !order.includes(draggedId)) return order.slice();
    next.splice(targetIndex + (after ? 1 : 0), 0, draggedId);
    return next;
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { moveBefore };
  }

  if (!root.document) return;
  const list = root.document.querySelector("[data-sortable-stops]");
  const form = root.document.getElementById("drag-order-form");
  if (!list || !form) return;

  const save = form.querySelector(".drag-save");
  const orderInput = form.querySelector('[name="ordered_ids"]');
  let dragged = null;

  function pendingItems() {
    return Array.from(list.querySelectorAll(':scope > [data-pending="true"]'));
  }

  function recordOrder() {
    orderInput.value = JSON.stringify(pendingItems().map((item) => item.dataset.stopId));
    save.hidden = false;
  }

  list.addEventListener("dragstart", (event) => {
    const item = event.target.closest('[data-pending="true"]');
    if (!item) return;
    dragged = item;
    item.classList.add("is-dragging");
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", item.dataset.stopId);
  });

  list.addEventListener("dragend", () => {
    if (dragged) dragged.classList.remove("is-dragging");
    dragged = null;
  });

  list.addEventListener("dragover", (event) => {
    if (dragged && event.target.closest('[data-pending="true"]')) event.preventDefault();
  });

  list.addEventListener("drop", (event) => {
    const target = event.target.closest('[data-pending="true"]');
    if (!dragged || !target || dragged === target) return;
    event.preventDefault();
    const bounds = target.getBoundingClientRect();
    const after = event.clientY > bounds.top + bounds.height / 2;
    const order = moveBefore(pendingItems().map((item) => item.dataset.stopId), dragged.dataset.stopId, target.dataset.stopId, after);
    const items = new Map(pendingItems().map((item) => [item.dataset.stopId, item]));
    order.forEach((id, index) => list.insertBefore(items.get(id), pendingItems()[index] || null));
    recordOrder();
  });
})(globalThis);
