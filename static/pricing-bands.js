function initializeZoneBandSelection(doc) {
  const boxes = [...doc.querySelectorAll(".zone-select")];
  const count = doc.getElementById("selected-zone-count");
  if (!boxes.length || !count) return;
  const update = () => { count.textContent = boxes.filter((box) => box.checked).length; };
  doc.getElementById("select-all-zones")?.addEventListener("click", () => {
    boxes.forEach((box) => { box.checked = true; });
    update();
  });
  doc.getElementById("clear-zones")?.addEventListener("click", () => {
    boxes.forEach((box) => { box.checked = false; });
    update();
  });
  boxes.forEach((box) => box.addEventListener("change", update));
  update();
}

if (typeof document !== "undefined") initializeZoneBandSelection(document);
if (typeof module !== "undefined") module.exports = { initializeZoneBandSelection };