/* Small adapters for locally served components. Keep the app's data in app.js. */
(() => {
  const listeners = new WeakMap();

  function listen(target, type, callback) {
    if (!target) return;
    let handlers = listeners.get(target);
    if (!handlers) listeners.set(target, handlers = new Map());
    if (handlers.has(type)) target.removeEventListener(type, handlers.get(type));
    handlers.set(type, callback);
    target.addEventListener(type, callback);
  }

  function dispose(root) {
    if (!root?.querySelectorAll) return;
    [root, ...root.querySelectorAll("*")].forEach((node) => {
      node._tippy?.destroy();
      node.tomselect?.destroy();
    });
  }

  function render(root, html) {
    const viewportTop = root.getBoundingClientRect().top;
    const anchor = root.scrollTop > 0 ? [...root.querySelectorAll("[data-import-row-id], [data-import-manual-source], [data-import-missing-user-row]")]
      .find((node) => node.getBoundingClientRect().bottom > viewportTop) : null;
    const anchorOffset = anchor ? anchor.getBoundingClientRect().top - viewportTop : 0;
    const template = document.createElement("template");
    template.innerHTML = html;
    // Stable identities let a refresh keep the same row, input and spinner.
    const keys = new Map();
    template.content.querySelectorAll("*").forEach((node) => {
      if (node.id) return;
      const key = [...node.attributes].find(({ name, value }) => value &&
        (name === "data-id" || /^data-(import|backlog)-/.test(name)));
      if (!key) return;
      const base = `${root.id}-${node.tagName}-${key.name}-${encodeURIComponent(key.value)}`;
      const count = keys.get(base) || 0;
      keys.set(base, count + 1);
      node.id = count ? `${base}-${count}` : base;
    });
    if (window.Idiomorph) {
      window.Idiomorph.morph(root, template.content, {
        morphStyle: "innerHTML",
        ignoreActiveValue: true,
        restoreFocus: true,
        callbacks: {
          beforeNodeRemoved(node) { dispose(node); },
          // Never change an open native picker underneath the keyboard.
          beforeNodeMorphed(oldNode) {
            return !(oldNode === document.activeElement && oldNode.tagName === "SELECT");
          },
        },
      });
    } else {
      dispose(root);
      root.replaceChildren(template.content);
    }
    if (anchor) {
      const retained = document.getElementById(anchor.id);
      if (retained && root.contains(retained)) root.scrollTop += retained.getBoundingClientRect().top - viewportTop - anchorOffset;
      else root.scrollTop = 0;
    }
  }

  const searchable = /^importMap(?!Sheet$)|^(cityInput|locationInput|pairsCityInput|pairsLocationInput|importCityInput|importLocationInput)$/;

  function enhanceSelect(select) {
    if (!window.TomSelect || !searchable.test(select.id) || select.tomselect || select.options.length < 8) return;
    const label = select.getAttribute("aria-label") || document.querySelector(`label[for="${select.id}"]`)?.textContent.trim()
      || select.closest("label")?.childNodes[0]?.textContent?.trim() || "Search options";
    new window.TomSelect(select, {
      create: false,
      maxOptions: null,
      selectOnTab: true,
      closeAfterSelect: true,
      dropdownParent: select.closest("dialog") || null,
      onInitialize() {
        this.control_input.setAttribute("aria-label", label);
        this.control_input.setAttribute("placeholder", "Search…");
        this.control_input.addEventListener("keydown", (event) => {
          // Enter chooses an option, not the containing sheet's default action.
          if (event.key === "Enter") { event.preventDefault(); event.stopPropagation(); }
        });
      },
    });
  }

  function selectOptions(select, html) {
    if (select.dataset.optionsHtml === html && select.innerHTML === select.dataset.renderedOptionsHtml) {
      const selected = select.querySelector("option[selected]") || select.options[0];
      const value = selected?.value || "";
      if (select.tomselect) select.tomselect.setValue(value, true);
      else select.value = value;
      return;
    }
    const picker = select.tomselect;
    const focused = picker?.wrapper.contains(document.activeElement);
    const query = focused ? picker.control_input.value : "";
    picker?.destroy();
    select.innerHTML = html;
    select.dataset.optionsHtml = html;
    enhanceSelect(select);
    select.dataset.renderedOptionsHtml = select.innerHTML;
    if (focused && select.tomselect) {
      select.tomselect.focus();
      select.tomselect.setTextboxValue(query);
      select.tomselect.refreshOptions(false);
    }
  }

  window.DeploymentComponents = { render, listen, selectOptions };
})();
