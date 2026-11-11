import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const cli = path.join(here, "../dist/cli.js");
const root = path.join(here, "fixtures/proj");
const run = (input) => spawnSync("node", [cli], { input: JSON.stringify(input), encoding: "utf8" });
const files = ["src/service.ts", "src/CheckoutPage.tsx", "src/PriceTag.tsx", "src/discount.ts", "src/lib/util.ts", "src/legacy.js"];
const out = JSON.parse(run({ root, files }).stdout);
const f = (p) => out.files.find((x) => x.path === p);

test("contract shape", () => {
  assert.equal(out.version, "1");
  assert.equal(out.files.length, files.length);
});
test("class method call resolved (dotted from_symbol)", () => {
  const c = f("src/service.ts").calls.find((x) => x.callee_text === "applyDiscount");
  assert.deepEqual(c.resolved, { path: "src/discount.ts", symbol: "applyDiscount" });
  assert.equal(c.from_symbol, "OrderService.checkout");
  const m = f("src/service.ts").symbols.find((s) => s.name === "checkout");
  assert.equal(m.kind, "method"); assert.equal(m.parent, "OrderService"); assert.equal(m.exported, true);
});
test("component usage resolved", () => {
  const u = f("src/CheckoutPage.tsx").component_usages[0];
  assert.equal(u.component, "PriceTag");
  assert.deepEqual(u.resolved, { path: "src/PriceTag.tsx", symbol: "PriceTag" });
  assert.equal(u.from_symbol, "CheckoutPage");
  assert.equal(f("src/CheckoutPage.tsx").symbols[0].kind, "component");
});
test("dynamic call and node_modules call are null", () => {
  const calls = f("src/service.ts").calls;
  assert.equal(calls.find((x) => x.callee_text === "handlers[name]").resolved, null);
  assert.equal(calls.find((x) => x.callee_text === "hello").resolved, null);
});
test("paths alias, node_modules import, allowJs", () => {
  const imps = f("src/service.ts").imports;
  assert.equal(imps.find((i) => i.specifier === "@lib/util").resolved_path, "src/lib/util.ts");
  assert.equal(imps.find((i) => i.specifier === "fakepkg").resolved_path, null);
  assert.equal(imps.find((i) => i.specifier === "./legacy.js").resolved_path, "src/legacy.js");
  const c = f("src/service.ts").calls.find((x) => x.callee_text === "legacyHelper");
  assert.deepEqual(c.resolved, { path: "src/legacy.js", symbol: "legacyHelper" });
  assert.equal(f("src/legacy.js").symbols[0].name, "legacyHelper");
});
test("empty files and invalid input", () => {
  assert.deepEqual(JSON.parse(run({ root, files: [] }).stdout), { version: "1", files: [], diagnostics: [] });
  const bad = run({ root: "rel", files: [] });
  assert.notEqual(bad.status, 0); assert.ok(bad.stderr.length > 0);
});
test("symlinks and non-regular files are never read (untrusted PR content)", async () => {
  const fs = await import("node:fs");
  const os = await import("node:os");
  const tmp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "tsa-")));
  try {
    const repo = path.join(tmp, "repo"); fs.mkdirSync(repo);
    fs.writeFileSync(path.join(tmp, "outside.ts"), "export function outsideSecretFn() { return 1 }\n");
    fs.symlinkSync(path.join(tmp, "outside.ts"), path.join(repo, "leak.ts"));
    fs.symlinkSync("/dev/zero", path.join(repo, "zero.ts"));
    spawnSync("mkfifo", [path.join(repo, "pipe.ts")]);
    fs.writeFileSync(path.join(repo, "main.ts"), 'import { outsideSecretFn } from "./leak"\nimport "./zero"\nimport "./pipe"\nexport const x = outsideSecretFn()\n');
    const t0 = Date.now();
    const r = spawnSync("node", [cli], { input: JSON.stringify({ root: repo, files: ["main.ts", "leak.ts", "zero.ts", "pipe.ts"] }), encoding: "utf8", timeout: 20000 });
    assert.equal(r.status, 0, r.stderr); assert.ok(Date.now() - t0 < 20000);
    const res = JSON.parse(r.stdout);
    assert.ok(!res.files.some((x) => x.symbols.some((s) => s.name === "outsideSecretFn")), "outside-root symbol leaked");
    assert.equal(res.files[0].calls.find((c) => c.callee_text === "outsideSecretFn").resolved, null);
    assert.deepEqual(res.files.map((x) => x.path), ["main.ts"]);
    assert.equal(res.files[0].imports.find((i) => i.specifier === "./leak").resolved_path, null);
    assert.ok(!JSON.stringify(res.files).includes("outside.ts"));
  } finally { fs.rmSync(tmp, { recursive: true, force: true }); }
});
