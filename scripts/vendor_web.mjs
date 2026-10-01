// Run after npm ci. The deployed app serves these files locally, without Node.
import { copyFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const root = new URL("../", import.meta.url);
for (const [source, target] of [
  ["idiomorph/dist/idiomorph.min.js", "idiomorph.min.js"],
  ["idiomorph/LICENSE", "IDIOMORPH_LICENSE"],
  ["tom-select/dist/js/tom-select.base.min.js", "tom-select.min.js"],
  ["tom-select/dist/css/tom-select.default.min.css", "tom-select.css"],
  ["tom-select/LICENSE", "TOM_SELECT_LICENSE"],
  ["tippy.js/dist/tippy-bundle.umd.min.js", "tippy-bundle.umd.min.js"],
  ["tippy.js/dist/tippy.css", "tippy.css"],
  ["tippy.js/animations/shift-away.css", "tippy-shift-away.css"],
  ["@popperjs/core/dist/umd/popper.min.js", "popper.min.js"],
  ["@popperjs/core/LICENSE.md", "POPPER_LICENSE"],
]) {
  copyFileSync(fileURLToPath(new URL(`node_modules/${source}`, root)),
    fileURLToPath(new URL(`web/vendor/${target}`, root)));
}
