// Render the diagram sources in src/*.jsx to static SVG files for the README.
//
// Usage: node docs/diagrams/render.mjs   (needs Node 18+ and network access for npx esbuild)
//
// Each source is a JSX function returning an <svg>. It is transpiled with esbuild and run with a
// tiny JSX factory that serializes to an SVG string. Colors in the sources are CSS variables;
// they move into style attributes, and each diagram is written twice, as <name>-light.svg and
// <name>-dark.svg, with the palette embedded in a <style>.

import { execFileSync } from "node:child_process";
import { mkdtempSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const ESBUILD = "esbuild@0.28.2";
const here = dirname(fileURLToPath(import.meta.url));
const srcDir = join(here, "src");

const FONT = `font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;`;
// One fixed palette per theme: the README picks a file with <picture>, following the reader's
// GitHub theme. A media query inside the SVG would follow the OS instead and can disagree.
const THEMES = {
  light: `svg { ${FONT}
  --cds-text-primary: #1f2328; --cds-text-secondary: #59636e; --cds-chart-axis: #8c959f;
  --cds-chart-grid: #d0d7de; --cds-chart-categorical-1: #0969da;
  --cds-chart-reference-tint: rgba(9, 105, 218, 0.06); }`,
  dark: `svg { ${FONT}
  --cds-text-primary: #e6edf3; --cds-text-secondary: #9198a1; --cds-chart-axis: #6e7681;
  --cds-chart-grid: #3d444d; --cds-chart-categorical-1: #4493f8;
  --cds-chart-reference-tint: rgba(68, 147, 248, 0.1); }`,
};

// SVG attributes that keep their camelCase spelling.
const KEEP_CASE = new Set(["viewBox", "refX", "refY", "markerWidth", "markerHeight"]);
// Presentation attributes cannot hold var(); these move into style instead.
const STYLE_PROPS = new Set(["fill", "stroke"]);

const escapeText = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const escapeAttr = (s) => escapeText(s).replace(/"/g, "&quot;");
const kebab = (name) => (KEEP_CASE.has(name) ? name : name.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`));

class Node {
  constructor(markup) {
    this.markup = markup;
  }
}

const serialize = (child) => {
  if (child === null || child === undefined || child === false) return "";
  if (Array.isArray(child)) return child.map(serialize).join("");
  if (child instanceof Node) return child.markup;
  return escapeText(child);
};

globalThis.h = (tag, props, ...children) => {
  const attrs = [];
  const style = [];
  for (const [key, value] of Object.entries(props ?? {})) {
    if (key.startsWith("data-") || value === undefined || value === null || value === false) continue;
    const name = kebab(key);
    if (STYLE_PROPS.has(name) && String(value).startsWith("var(")) style.push(`${name}:${value}`);
    else attrs.push(`${name}="${escapeAttr(value)}"`);
  }
  if (style.length) attrs.push(`style="${style.join(";")}"`);
  const open = `<${[tag, ...attrs].join(" ")}`;
  const inner = serialize(children);
  return new Node(inner ? `${open}>${inner}</${tag}>` : `${open}/>`);
};

const work = mkdtempSync(join(tmpdir(), "saddlefit-diagrams-"));
try {
  for (const file of readdirSync(srcDir).filter((f) => f.endsWith(".jsx")).sort()) {
    const out = join(work, basename(file, ".jsx") + ".mjs");
    execFileSync("npx", ["--yes", ESBUILD, join(srcDir, file), "--loader:.jsx=jsx", "--jsx-factory=h", "--format=esm", `--outfile=${out}`, "--log-level=warning"], { stdio: "inherit" });
    const { default: render } = await import(pathToFileURL(out).href);
    const markup = render().markup.replace("<svg ", `<svg xmlns="http://www.w3.org/2000/svg" width="760" `);
    for (const [theme, css] of Object.entries(THEMES)) {
      const target = join(here, `${basename(file, ".jsx")}-${theme}.svg`);
      writeFileSync(target, markup.replace(/(<svg[^>]*>)/, `$1<style>${css}</style>`) + "\n");
      console.log(`wrote ${target}`);
    }
  }
} finally {
  rmSync(work, { recursive: true, force: true });
}
