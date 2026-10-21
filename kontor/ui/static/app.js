// Editor: Zeilen hinzufügen/entfernen, Standardposition übernehmen, Summen live (htmx), Bestätigung vor Unumkehrbarem.
document.addEventListener("click", (e) => {
  const add = e.target.closest("[data-add-row]");
  if (add) {
    const body = document.querySelector(add.dataset.addRow);
    const tpl = document.querySelector(add.dataset.template);
    body.appendChild(tpl.content.cloneNode(true));
    body.dispatchEvent(new Event("change", { bubbles: true }));
  }
  const del = e.target.closest("[data-remove-row]");
  if (del) {
    const form = del.closest("form");
    del.closest("tr").remove();
    form.dispatchEvent(new Event("change", { bubbles: true }));
  }
  const pick = e.target.closest("[data-catalog]");
  if (pick) {
    const item = JSON.parse(pick.dataset.catalog);
    const body = document.querySelector("#lines");
    body.appendChild(document.querySelector("#line-template").content.cloneNode(true));
    const row = body.lastElementChild;
    row.querySelector("[name=line_title]").value = item.title;
    row.querySelector("[name=line_description]").value = item.description;
    row.querySelector("[name=line_price]").value = item.price;
    row.querySelector("[name=line_tax]").value = item.tax_rate;
    document.querySelector("#catalog-results").innerHTML = "";
    body.dispatchEvent(new Event("change", { bubbles: true }));
  }
});
document.addEventListener("submit", (e) => {
  const msg = (e.submitter && e.submitter.dataset.ask) || e.target.dataset.ask;
  if (msg && !window.confirm(msg)) e.preventDefault();
});

// Drag & Drop: Dateien irgendwo ins Fenster ziehen → landen als Belege im Eingang.
(() => {
  let depth = 0;
  const overlay = () => document.getElementById("drop-overlay");
  const hasFiles = (e) => e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files");
  window.addEventListener("dragenter", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth += 1;
    overlay()?.classList.add("on");
  });
  window.addEventListener("dragover", (e) => { if (hasFiles(e)) e.preventDefault(); });
  window.addEventListener("dragleave", (e) => {
    if (!hasFiles(e)) return;
    depth = Math.max(0, depth - 1);
    if (!depth) overlay()?.classList.remove("on");
  });
  window.addEventListener("drop", async (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth = 0;
    overlay()?.classList.add("busy");
    const form = new FormData();
    for (const f of e.dataTransfer.files) form.append("dateien", f, f.name);
    try {
      const r = await fetch(document.body.dataset.base + "/belege/upload", { method: "POST", body: form });
      window.location = r.url;
    } catch (err) {
      overlay()?.classList.remove("on", "busy");
      alert("Hochladen fehlgeschlagen: " + err);
    }
  });
})();

// Live-Update: lädt die Seite neu, sobald der Server mit neuem Code läuft. Nicht, während ein Formular bearbeitet wird.
(() => {
  let boot = null;
  let dirty = false;
  document.addEventListener("input", () => { dirty = true; });
  setInterval(async () => {
    try {
      const r = await fetch("/api/boot", { cache: "no-store" });
      const id = (await r.json()).boot;
      if (boot && id !== boot && !dirty) window.location.reload();
      boot = id;
    } catch (e) { /* Server startet gerade neu */ }
  }, 2000);
})();

// Standardpositionen: Titel aus der Vorschlagsliste übernimmt Beschreibung, Preis und Steuersatz (nur leere Felder).
document.addEventListener("change", (e) => {
  const input = e.target;
  if (input.name !== "line_title" || !window.KATALOG) return;
  const item = window.KATALOG[input.value];
  const row = input.closest("tr");
  if (!item || !row) return;
  const set = (name, value) => { const f = row.querySelector(`[name=${name}]`); if (f && !f.value) f.value = value; };
  set("line_description", item.description);
  set("line_price", item.price);
  const tax = row.querySelector("[name=line_tax]");
  if (tax) tax.value = item.tax_rate;
  row.closest("form")?.dispatchEvent(new Event("change", { bubbles: true }));
});

// Reihenfolge der Standardpositionen per Drag & Drop.
(() => {
  const body = document.getElementById("catalog-rows");
  if (!body) return;
  let dragged = null;
  body.addEventListener("dragstart", (e) => { dragged = e.target.closest("tr[data-item]"); dragged?.classList.add("dragging"); });
  body.addEventListener("dragover", (e) => {
    const over = e.target.closest("tr[data-item]");
    if (!dragged || !over || over === dragged) return;
    e.preventDefault();
    const after = e.clientY > over.getBoundingClientRect().top + over.offsetHeight / 2;
    over.parentNode.insertBefore(dragged, after ? over.nextSibling : over);
  });
  body.addEventListener("dragend", async () => {
    if (!dragged) return;
    dragged.classList.remove("dragging");
    dragged = null;
    const ids = Array.from(body.querySelectorAll("tr[data-item]")).map((r) => r.dataset.item).join(",");
    const form = new FormData();
    form.append("ids", ids);
    await fetch(document.body.dataset.base + "/positionen/reihenfolge", { method: "POST", body: form });
  });
})();

// PDF speichern unter …: Finder-Dialog zur Ordnerwahl, vorbelegt mit dem Kundenordner.
document.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-choose]");
  if (!btn) return;
  const form = btn.closest("form.save-pdf");
  if (!form) return;
  e.preventDefault();
  btn.disabled = true;
  try {
    const body = new FormData();
    body.append("start", form.dataset.folder || "");
    const r = await fetch(document.body.dataset.base + "/ordner-waehlen", { method: "POST", body });
    const path = (await r.json()).path;
    if (!path) return;                      // abgebrochen
    form.querySelector("[name=ziel]").value = path;
    form.submit();
  } finally {
    btn.disabled = false;
  }
});

// Mac-App: Titelleiste zeigt den Mandanten (Spec 18.2).
window.addEventListener("pywebviewready", () => {
  if (window.pywebview.api.set_title) window.pywebview.api.set_title(document.title);
});
