// @vitest-environment node
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import postcss from "postcss";
import { beforeAll, describe, expect, it } from "vitest";
import config from "../../postcss.config.js";

let compiled: postcss.Root;

beforeAll(async () => {
  const plugins = await Promise.all(
    Object.entries(config.plugins).map(async ([name, options]) => {
      const { default: plugin } = await import(name);
      return plugin(options);
    }),
  );
  const path = fileURLToPath(new URL("./globals.css", import.meta.url));
  compiled = (await postcss(plugins).process(await readFile(path, "utf8"), { from: path })).root;
});

function declarations(selector: string) {
  const values: Record<string, string> = {};
  compiled.walkRules(selector, (rule) => {
    for (const node of rule.nodes) {
      if (node.type === "decl") values[node.prop] = node.value;
    }
  });
  return values;
}

describe("Tailwind 4 application styling", () => {
  it("test_preserves_the_custom_surface_and_accent_colors", () => {
    expect(declarations(".bg-surface-1")["background-color"]).toBe("#111111");
    expect(declarations(".text-accent").color).toBe("#6366f1");
  });

  it("test_composes_the_shared_button_utility_with_variant_colors", () => {
    const primary = declarations(".btn-primary");
    const secondary = declarations(".btn-secondary");
    expect(primary["background-color"]).toBe("#6366f1");
    expect(secondary["background-color"]).toBe("#222222");
    expect(primary["padding-inline"]).toBeTruthy();
    expect(secondary["padding-inline"]).toBe(primary["padding-inline"]);
    expect(primary["border-radius"]).toBeTruthy();
  });

  it("test_preserves_the_disabled_select_dimming", () => {
    expect(declarations("select.input:disabled").opacity).toBe("0.7");
  });

  it("test_preserves_input_borders_and_the_button_pointer", () => {
    const input = declarations(".input");
    expect(input["background-color"]).toBe("#1a1a1a");
    expect(input["border-color"]).toBe("#2a2a2a");
    expect(compiled.toString()).toMatch(/cursor:\s*pointer/);
  });
});
