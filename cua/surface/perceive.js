// Injected into every frame. Two jobs: reading the page, and recording clicks.
//
// perceive() turns whatever markup the page has into a flat list of controls, named
// the way an operator would: role + visible label (+ table row/column). It doesn't
// rely on ids, test ids or <label> tags, because old pages usually put the label in
// the table cell next to the input, so we work out that "visual label" too.
//
// The recorder describes human clicks/edits the same way, so whatever a human does
// during a handoff lines up with the recorded steps.
(() => {
  if (window.__cua) return;
  const norm = (s) => (s || "").replace(/\s+/g, " ").trim();
  const stripColon = (s) => s.replace(/\s*:\s*$/, "");

  function visible(el) {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const st = getComputedStyle(el);
    return st.visibility !== "hidden" && st.display !== "none";
  }

  function roleOf(el) {
    const explicit = el.getAttribute("role");
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === "a" && el.hasAttribute("href")) return "link";
    if (tag === "button") return "button";
    if (tag === "select") return "combobox";
    if (tag === "textarea") return "textbox";
    if (tag === "input") {
      const t = (el.getAttribute("type") || "text").toLowerCase();
      if (["submit", "button", "reset", "image"].includes(t)) return "button";
      if (t === "checkbox") return "checkbox";
      if (t === "radio") return "radio";
      if (t === "password") return "password";
      if (t === "hidden") return null;
      return "textbox";
    }
    if (tag === "td") return "cell";
    return null;
  }

  function visualLabel(el) {
    const td = el.closest("td,th");
    if (td) {
      for (let p = td.previousElementSibling; p; p = p.previousElementSibling) {
        const t = norm(p.innerText);
        if (t) return stripColon(t);
      }
    }
    for (let n = el.previousSibling; n; n = n.previousSibling) {
      const t = norm(n.textContent);
      if (t) return stripColon(t);
    }
    return "";
  }

  function nameOf(el, role) {
    const aria = el.getAttribute("aria-label");
    if (aria) return [norm(aria), "aria-label"];
    const lb = el.getAttribute("aria-labelledby");
    if (lb) {
      const t = lb.split(/\s+/).map((i) => document.getElementById(i)).filter(Boolean)
        .map((e) => norm(e.innerText)).join(" ");
      if (t) return [t, "aria-labelledby"];
    }
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l && norm(l.innerText)) return [stripColon(norm(l.innerText)), "label"];
    }
    const wrap = el.closest("label");
    if (wrap && norm(wrap.innerText)) return [stripColon(norm(wrap.innerText)), "label"];
    if (role === "button") {
      const v = el.tagName === "INPUT" ? el.value : el.innerText;
      if (norm(v)) return [norm(v), "content"];
    }
    if (role === "link" || role === "cell") {
      if (norm(el.innerText)) return [norm(el.innerText), "content"];
    }
    const vl = visualLabel(el);
    if (vl) return [vl, "visual-label"];
    if (el.title) return [norm(el.title), "title"];
    if (el.placeholder) return [norm(el.placeholder), "placeholder"];
    return ["", "none"];
  }

  function isBold(el) {
    return parseInt(getComputedStyle(el).fontWeight, 10) >= 600 || !!el.querySelector("b,strong");
  }

  function tableCtx(el, tables) {
    const td = el.closest("td,th");
    if (!td) return {};
    const tr = td.parentElement;
    const table = tr && tr.closest("table");
    if (!table) return {};
    const cells = [...tr.children].filter((c) => c.tagName === "TD" || c.tagName === "TH");
    const idx = cells.indexOf(td);
    const rows = [...table.rows];
    let column = null;
    const first = rows[0];
    if (first && first !== tr) {
      const fc = [...first.cells];
      const header = fc.length >= 2 && fc.length === cells.length && fc.every((c) => c.tagName === "TH" || isBold(c));
      if (header && fc[idx]) column = norm(fc[idx].innerText) || null;
    }
    return {
      row: cells.map((c) => norm(c.innerText)),
      column,
      col_index: idx,
      table: tables.indexOf(table),
      row_index: rows.indexOf(tr),
    };
  }

  function describe(el, tables) {
    const role = roleOf(el);
    if (!role) return null;
    const [name, source] = nameOf(el, role);
    let value = null;
    let options = null;
    if (role === "textbox") value = el.value;
    if (role === "password") value = el.value ? "********" : "";
    if (role === "combobox") {
      options = [...el.options].map((o) => norm(o.text));
      value = el.selectedIndex >= 0 ? norm(el.options[el.selectedIndex].text) : "";
    }
    if (role === "checkbox" || role === "radio") value = el.checked ? "checked" : "unchecked";
    return {
      role, name, name_source: source, value, options,
      enabled: !el.disabled,
      native_id: el.getAttribute("name") || el.id || null,
      ...tableCtx(el, tables),
    };
  }

  const INTERACTIVE = "a[href],button,input,select,textarea,[role=button],[role=link]";

  function perceive(prefix) {
    const tables = [...document.querySelectorAll("table")];
    const out = [];
    document.querySelectorAll("[data-cua-ref]").forEach((e) => e.removeAttribute("data-cua-ref"));
    const els = [...document.querySelectorAll(INTERACTIVE)];
    // Plain data cells too (no nested tables or inputs), so values can be read by row/column.
    document.querySelectorAll("td").forEach((td) => {
      if (!td.querySelector("table,input,select,textarea,a,button") && norm(td.innerText)) els.push(td);
    });
    let i = 0;
    for (const el of els) {
      if (!visible(el)) continue;
      const d = describe(el, tables);
      if (!d) continue;
      const ref = `${prefix}${i++}`;
      el.setAttribute("data-cua-ref", ref);
      d.ref = ref;
      out.push(d);
    }
    const headings = [];
    document.querySelectorAll("h1,h2,h3,h4,b,strong,font,span,div").forEach((el) => {
      const t = norm(el.innerText);
      if (!t || t.length > 80 || !visible(el)) return;
      const tag = el.tagName.toLowerCase();
      const big = parseFloat(getComputedStyle(el).fontSize) >= 16 && isBold(el);
      if (/^h[1-4]$/.test(tag) || (big && !el.closest("a,button,td.hdr"))) {
        if (!headings.includes(t)) headings.push(t);
      }
    });
    const body = document.body;
    return {
      controls: out,
      headings,
      title: document.title,
      text: body ? norm(body.innerText).slice(0, 6000) : "",
    };
  }

  function recordEvent(type, target) {
    if (!window.__cuaRecord) return;
    const el = target.closest(INTERACTIVE) || target.closest("td");
    if (!el) return;
    const d = describe(el, [...document.querySelectorAll("table")]);
    if (!d) return;
    try { window.__cuaRecord({ type, ...d }); } catch (e) { /* binding not present */ }
  }

  document.addEventListener("click", (e) => recordEvent("click", e.target), true);
  document.addEventListener("change", (e) => recordEvent("change", e.target), true);

  window.__cua = { perceive };
})();
